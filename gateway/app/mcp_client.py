"""MCP client — the gateway consuming another service's tools.

The gateway has governed *prompts* since day one. It has not governed *tools*, and a
gateway that screens what a user types while a model freely pulls account data through a
side channel is governing the smaller half of the traffic. This module is the client
half; `main.py` applies the controls around it.

Transport is streamable HTTP, which is what MCP offers for a remote server and the only
one that works across a service boundary — stdio needs a subprocess, and a Cloud Run
container is not going to spawn one belonging to another team.

**Authentication is Cloud Run IAM, not MCP.** MCP's own answer is OAuth with dynamic
client registration, which nothing here implements. So the target service is private and
this gateway holds `roles/run.invoker` on it, minting an OIDC id-token per call. The
consequence is worth stating plainly rather than discovering later: the remote server
sees *this gateway's* identity, not the signed-in human's, so anything it masks per-user
is masked according to the gateway's entitlements. Tool results are scoped to a service;
the person's own entitlements are enforced here, by persona, before the call is made.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time

# JSON Schema is a superset of what Vertex accepts in a functionDeclaration, and Vertex
# rejects the extras outright rather than ignoring them: one stray `additionalProperties`
# from a server you do not control fails the whole request with a 400 naming a field you
# never wrote. Whitelist rather than blacklist — a new keyword upstream should degrade to
# "not forwarded", never to "every tool call breaks".
_SCHEMA_KEYS = {"type", "description", "enum", "items", "properties", "required",
                "nullable", "format", "minItems", "maxItems"}
_TYPES = {"string": "STRING", "integer": "INTEGER", "number": "NUMBER",
          "boolean": "BOOLEAN", "array": "ARRAY", "object": "OBJECT"}

_CACHE: dict[str, tuple[float, tuple]] = {}
_TOKENS: dict[str, tuple[str, float]] = {}


class MCPUnavailable(RuntimeError):
    """The MCP server could not be reached, or refused the connection."""


def servers() -> dict[str, str]:
    """Registered MCP servers as name -> base URL. Deployment config, never code.

        MCP_SERVERS="finchat=https://finchat-dev-mcp-xxxxx-uc.a.run.app"

    An empty registry is a working gateway with the tool surface switched off, which is
    the right default: a tool nobody asked for is reach nobody granted.
    """
    out: dict[str, str] = {}
    for entry in os.getenv("MCP_SERVERS", "").split(";"):
        name, _, url = entry.partition("=")
        if name.strip() and url.strip():
            out[name.strip()] = url.strip().rstrip("/")
    return out


def _id_token(audience: str) -> str | None:
    """An OIDC id-token for a private Cloud Run audience.

    Metadata identity endpoint first. `google.oauth2.id_token.fetch_id_token` reaches
    the same credentials on Cloud Run — it pings the metadata server itself — so this is
    a choice about how failure reads, not about what works. `fetch_id_token` catches the
    ImportError raised when `google-auth`'s requests transport has no `requests` package
    and reports "Neither metadata server or valid service account credentials are
    found": a sentence about IAM describing a missing dependency. The gcloud fallback is
    for a developer running this locally against the real endpoint.
    """
    hit = _TOKENS.get(audience)
    if hit and hit[1] > time.time():
        return hit[0]

    token = None
    try:
        from google.auth import compute_engine
        from google.auth.transport.requests import Request as GReq

        creds = compute_engine.IDTokenCredentials(
            GReq(), target_audience=audience, use_metadata_identity_endpoint=True)
        creds.refresh(GReq())
        token = creds.token
    except Exception:
        token = None

    if not token:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import id_token as gid

            token = gid.fetch_id_token(Request(), audience)
        except Exception:
            token = None

    if not token and shutil.which("gcloud"):
        try:
            out = subprocess.run(["gcloud", "auth", "print-identity-token"],
                                 capture_output=True, text=True, timeout=30,
                                 stdin=subprocess.DEVNULL)
            token = out.stdout.strip() if out.returncode == 0 else None
        except Exception:
            token = None

    if token:
        _TOKENS[audience] = (token, time.time() + 45 * 60)
    else:
        # The one thing this function must never do is fail quietly. A bare None here
        # sends the request out with no Authorization header, and the 403 that comes
        # back reads as "the MCP server is broken" rather than "we never authenticated".
        print(f"mcp: no id-token for {audience} — the call goes out unauthenticated")
    return token


def to_vertex_schema(schema: dict | None) -> dict:
    """One MCP JSON Schema, reduced to the subset Vertex accepts."""
    if not isinstance(schema, dict):
        return {"type": "OBJECT", "properties": {}}
    out: dict = {}
    for key, value in schema.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key == "type":
            raw = value[0] if isinstance(value, list) and value else value
            out["type"] = _TYPES.get(str(raw).lower(), "STRING")
        elif key == "properties" and isinstance(value, dict):
            out["properties"] = {n: to_vertex_schema(s) for n, s in value.items()}
        elif key == "items":
            out["items"] = to_vertex_schema(value)
        else:
            out[key] = value
    out.setdefault("type", "OBJECT")
    # Vertex rejects an OBJECT schema with no properties, which is exactly how a
    # zero-argument tool arrives. An empty property bag is the accepted spelling.
    if out["type"] == "OBJECT":
        out.setdefault("properties", {})
    return out


class Catalog:
    """What a server offers: its tools, and the instructions it asks clients to honour."""

    def __init__(self, name: str, url: str, tools: list, instructions: str | None):
        self.name = name
        self.url = url
        self.tools = tools
        self.instructions = instructions or ""

    def declarations(self, allow: set[str] | None = None) -> list[dict]:
        return [{"name": t["name"], "description": t["description"],
                 "parameters": t["parameters"]}
                for t in self.tools if allow is None or t["name"] in allow]

    def names(self) -> list[str]:
        return [t["name"] for t in self.tools]


async def _discover(url: str) -> tuple[list, str | None]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {}
    token = _id_token(url)
    if token:
        headers["Authorization"] = "Bearer " + token
    async with streamablehttp_client(url + "/mcp", headers=headers, timeout=90) as (r, w, _):
        async with ClientSession(r, w) as session:
            init = await session.initialize()
            listed = await session.list_tools()
            tools = [{"name": t.name,
                      "description": (t.description or "").strip()[:1024],
                      "parameters": to_vertex_schema(t.inputSchema)}
                     for t in listed.tools]
            return tools, getattr(init, "instructions", None)


def catalog(name: str, ttl: float = 300.0) -> Catalog:
    """Discover a server's tools, cached briefly.

    Cached because discovery is a full connect + initialize handshake against a
    scale-to-zero service, and paying a cold start on every question would make the tool
    path look slow for a reason that has nothing to do with tools. The TTL is short
    because a server that gains a tool should not need this one redeployed to see it.
    """
    url = servers().get(name)
    if not url:
        raise MCPUnavailable("No MCP server registered as " + repr(name) + ". Set MCP_SERVERS.")
    hit = _CACHE.get(name)
    if hit and hit[0] > time.time():
        return Catalog(name, url, hit[1][0], hit[1][1])
    try:
        tools, instructions = asyncio.run(_discover(url))
    except Exception as exc:
        raise MCPUnavailable(name + ": " + type(exc).__name__ + ": " + str(exc)) from None
    _CACHE[name] = (time.time() + ttl, (tools, instructions))
    return Catalog(name, url, tools, instructions)


async def _call(url: str, calls: list) -> list[str]:
    """Execute a batch of tool calls on one session.

    One session for the batch rather than one per call: `initialize` is a round trip to a
    service that may be cold, and a three-tool answer paying that three times is the
    difference between a demo that feels instant and one that does not.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {}
    token = _id_token(url)
    if token:
        headers["Authorization"] = "Bearer " + token
    out: list[str] = []
    async with streamablehttp_client(url + "/mcp", headers=headers, timeout=90) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            for name, args in calls:
                try:
                    res = await session.call_tool(name, args or {})
                    text = "\n".join(c.text for c in res.content
                                     if getattr(c, "type", "") == "text")
                    # A tool that refuses is not a transport failure. The MCP server's
                    # refusal policy is the bank's policy, so a refusal has to reach the
                    # model as the tool's answer — turning it into an exception here
                    # would invite a retry or, worse, a guess.
                    out.append(text or "(the tool returned no text)")
                except Exception as exc:
                    out.append(json.dumps({"error": type(exc).__name__ + ": " + str(exc)}))
    return out


def call_tools(cat: Catalog, calls: list) -> list[str]:
    """Run `[(tool_name, args), ...]` and return one text result per call, in order."""
    if not calls:
        return []
    try:
        return asyncio.run(_call(cat.url, calls))
    except Exception as exc:
        raise MCPUnavailable(cat.name + ": " + type(exc).__name__ + ": " + str(exc)) from None
