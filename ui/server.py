"""Bank AI Gateway UI — serves the sign-in SPA and proxies chat traffic.

Auth (FinChat ADR-0016 pattern): the SPA obtains a Google Identity Services
ID token; every API call carries it as `Authorization: Bearer <token>`. This
server verifies the token (signature, audience = our OAuth client, expiry,
email_verified) and forwards the VERIFIED email to the private gateway as
user_id — the browser never talks to the gateway and never chooses its own
identity. Persona entitlements are resolved gateway-side.

Local dev: with GOOGLE_OAUTH_CLIENT_ID unset, sign-in is bypassed and the SPA
offers the demo users (the gateway's dev persona mapping handles them)."""
import os
import shutil
import subprocess
import time
from pathlib import Path

import requests
from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080")
OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Bank AI Gateway UI")

# ── Google sign-in verification (cached; GIS tokens live ~1h) ───────────────
_user_cache: dict[str, dict] = {}


def _verify(token: str) -> dict | None:
    """Returns {email, exp} for a valid GIS ID token, else None."""
    cached = _user_cache.get(token)
    if cached and cached["exp"] > time.time():
        return cached
    try:
        import google.auth.transport.requests
        from google.oauth2 import id_token as gid
        info = gid.verify_oauth2_token(
            token, google.auth.transport.requests.Request(), OAUTH_CLIENT_ID)
        if not info.get("email_verified"):
            return None
        user = {"email": (info.get("email") or "").lower(), "exp": info["exp"]}
        _user_cache[token] = user
        return user
    except Exception:
        return None


def _identity(authorization: str | None) -> str | None:
    """Resolve the caller's identity: verified email, or a demo id in dev mode."""
    if not OAUTH_CLIENT_ID:  # local dev — no sign-in configured
        return (authorization or "").removeprefix("Bearer dev:") or None
    if not authorization or not authorization.startswith("Bearer "):
        return None
    user = _verify(authorization.removeprefix("Bearer "))
    return user["email"] if user else None


# ── Gateway proxy (service-to-service auth via ID token) ────────────────────
def _jwt(stdout: str) -> str | None:
    """The JWT in a command's stdout, ignoring anything else it printed.

    `stdout.strip()` looks obviously right and is not. The gcloud launcher on Windows
    can emit a stray line — a temp-file path, in the case that produced this — before
    the token, and the whole blob then goes into an Authorization header. The failure is
    a 401, or `InvalidHeader: return character(s) in header value`, neither of which
    mentions gcloud.

    A JWT is three base64url segments and no spaces. Match that rather than trusting a
    subprocess to print only what you asked for.
    """
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.count(".") == 2 and " " not in line and len(line) > 100:
            return line
    return None


def _gateway_headers() -> dict:
    """An OIDC id-token for the private gateway, however this process is running.

    On Cloud Run `fetch_id_token` is enough: it pings the metadata server and mints a
    token for the service's own identity. It cannot do that for a signed-in *human*, so
    running this UI on a laptop against a deployed gateway needs the token gcloud
    already holds — which Cloud Run accepts from anyone with run.invoker, and which has
    the side benefit that the call is attributable to a person rather than a shared
    identity.

    `stdin=DEVNULL` is not hygiene. `subprocess.run` inherits stdin by default, and a
    server that ever runs over a stdio protocol would have gcloud eat its input stream.
    That cost a day to find once already, in the MCP server next door.
    """
    if GATEWAY_URL.startswith("http://localhost"):
        return {}
    try:
        import google.auth.transport.requests
        import google.oauth2.id_token
        token = google.oauth2.id_token.fetch_id_token(
            google.auth.transport.requests.Request(), GATEWAY_URL)
        return {"Authorization": f"Bearer {token}"}
    except Exception as e:
        # Bound to a second name on purpose: `except ... as workload_err` UNBINDS the
        # name at the end of the handler, so the reference below would be a NameError —
        # on exactly the failure path that exists to explain a failure.
        workload_err = e

    gcloud = shutil.which("gcloud")
    if gcloud:
        try:
            out = subprocess.run([gcloud, "auth", "print-identity-token"],
                                 capture_output=True, text=True, timeout=30,
                                 stdin=subprocess.DEVNULL)
            token = _jwt(out.stdout) if out.returncode == 0 else None
            if token:
                return {"Authorization": f"Bearer {token}"}
        except Exception:
            pass

    # Never silently. An empty header set produces a 403 from the gateway, which reads
    # as "the gateway rejected this user" rather than "we never authenticated at all".
    print(f"ui: no id-token for {GATEWAY_URL} ({type(workload_err).__name__}: "
          f"{workload_err}) — the request goes out unauthenticated")
    return {}


# ── Routes ───────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return html.replace("{{CLIENT_ID}}", OAUTH_CLIENT_ID)


@app.get("/api/me")
def me(authorization: str | None = Header(default=None)):
    user = _identity(authorization)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    r = requests.get(f"{GATEWAY_URL}/v1/me/{user}",
                     headers=_gateway_headers(), timeout=15)
    return JSONResponse({"email": user, **r.json()}, status_code=r.status_code)


@app.get("/api/conversations")
def conversations(authorization: str | None = Header(default=None)):
    user = _identity(authorization)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    r = requests.get(f"{GATEWAY_URL}/v1/conversations/{user}",
                     headers=_gateway_headers(), timeout=15)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/api/conversations/{conv_id}")
def conversation(conv_id: str, authorization: str | None = Header(default=None)):
    user = _identity(authorization)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    r = requests.get(f"{GATEWAY_URL}/v1/conversations/{user}/{conv_id}",
                     headers=_gateway_headers(), timeout=15)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.put("/api/context")
async def put_context(request: Request, authorization: str | None = Header(default=None)):
    user = _identity(authorization)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    r = requests.put(f"{GATEWAY_URL}/v1/context/{user}", json=body,
                     headers=_gateway_headers(), timeout=15)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/api/memories")
def memories(authorization: str | None = Header(default=None)):
    user = _identity(authorization)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    r = requests.get(f"{GATEWAY_URL}/v1/memories/{user}",
                     headers=_gateway_headers(), timeout=15)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: str, authorization: str | None = Header(default=None)):
    user = _identity(authorization)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    r = requests.delete(f"{GATEWAY_URL}/v1/memories/{user}/{memory_id}",
                        headers=_gateway_headers(), timeout=15)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/api/chat")
async def chat(request: Request, authorization: str | None = Header(default=None)):
    user = _identity(authorization)
    if user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    payload = {"user_id": user, "message": body.get("message", "")}
    if body.get("tier") in ("standard", "premium"):
        payload["tier"] = body["tier"]

    # Two surfaces, one box. With tools on, the question goes to the MCP surface, which
    # answers from a remote server's tools or refuses; with it off, the model answers
    # from its own knowledge. Keeping them visibly separate is the point of the toggle:
    # "where did this answer come from" should never be a guess.
    if body.get("tools"):
        payload["server"] = body.get("server") or "finchat"
        r = requests.post(f"{GATEWAY_URL}/v1/mcp/chat", json=payload,
                          headers=_gateway_headers(), timeout=180)
        return JSONResponse(r.json(), status_code=r.status_code)

    if body.get("conversation_id"):
        payload["conversation_id"] = body["conversation_id"]
    r = requests.post(f"{GATEWAY_URL}/v1/chat", json=payload,
                      headers=_gateway_headers(), timeout=120)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/api/mcp/servers")
def mcp_servers(authorization: str | None = Header(default=None)):
    """Live tool discovery, so the UI can say what is reachable before anyone asks."""
    if _identity(authorization) is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    r = requests.get(f"{GATEWAY_URL}/v1/mcp/servers",
                     headers=_gateway_headers(), timeout=90)
    return JSONResponse(r.json(), status_code=r.status_code)
