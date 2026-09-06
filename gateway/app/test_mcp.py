"""Tests for the governed MCP surface.

Every test here pins a claim the module's docstring makes. A comment saying tool results
are screened is worth nothing; a test that fails when the screen is removed is the claim.

Runs fully offline: no GCP project, no MCP server, no model. `passthrough.generate` and
`mcp_client` are the two seams, and both are replaced.
"""
from types import SimpleNamespace

import pytest

from . import audit, mcp_client, mcp_endpoint
from .guards import budget


def _req(**kw):
    base = {"user_id": "demo-manager", "message": "What is the balance on acct-001?",
            "server": "finchat", "tier": None, "max_tool_turns": 4}
    base.update(kw)
    return SimpleNamespace(**base)


def _catalog(instructions="Never give individualised financial advice."):
    return mcp_client.Catalog(
        "finchat", "https://example.invalid",
        [{"name": "get_account_balance", "description": "Balance for an account.",
          "parameters": {"type": "OBJECT", "properties": {
              "account_id": {"type": "STRING"}}, "required": ["account_id"]}}],
        instructions)


def _model_reply(text=None, calls=(), in_tok=100, out_tok=20):
    parts = [{"text": text}] if text else [
        {"functionCall": {"name": n, "args": a}} for n, a in calls]
    return {"candidates": [{"content": {"role": "model", "parts": parts}}],
            "usageMetadata": {"promptTokenCount": in_tok, "candidatesTokenCount": out_tok}}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Never touch the real audit log or a real budget store during a test."""
    monkeypatch.setattr(audit, "LOCAL_AUDIT_LOG", tmp_path / "audit.jsonl")
    monkeypatch.setattr(audit, "GCP_PROJECT", "")
    budget._local_usage.clear()
    mcp_client._CACHE.clear()


def _events(tmp_path):
    import json
    log = tmp_path / "audit.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


# --- schema translation ------------------------------------------------------
def test_unsupported_json_schema_keywords_are_dropped_not_forwarded():
    """Vertex 400s on keywords it does not know, naming a field the author never wrote.

    A server you do not control decides what its schemas contain, so the failure mode of
    an unknown keyword must be "not forwarded", never "every tool call breaks".
    """
    out = mcp_client.to_vertex_schema({
        "type": "object",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "title": "GetBalanceArguments",
        "properties": {"account_id": {"type": "string", "default": "acct-001"}},
        "required": ["account_id"],
    })
    assert out == {"type": "OBJECT", "properties": {"account_id": {"type": "STRING"}},
                   "required": ["account_id"]}


def test_a_zero_argument_tool_still_gets_a_property_bag():
    """Vertex rejects an OBJECT schema with no `properties`, which is how `finchat_status`
    and every other argument-free tool arrives."""
    assert mcp_client.to_vertex_schema({"type": "object"}) == {
        "type": "OBJECT", "properties": {}}


def test_nested_array_item_schemas_are_translated_too():
    out = mcp_client.to_vertex_schema({
        "type": "object",
        "properties": {"ids": {"type": "array", "items": {"type": "string",
                                                          "pattern": "^acct"}}}})
    assert out["properties"]["ids"] == {"type": "ARRAY", "items": {"type": "STRING"}}


def test_server_registry_is_parsed_from_deployment_config(monkeypatch):
    monkeypatch.setenv("MCP_SERVERS", "finchat=https://a.example/ ; other=https://b.example")
    assert mcp_client.servers() == {"finchat": "https://a.example",
                                    "other": "https://b.example"}


def test_no_registered_servers_means_no_tool_surface(monkeypatch):
    monkeypatch.setenv("MCP_SERVERS", "")
    assert mcp_client.servers() == {}
    with pytest.raises(mcp_client.MCPUnavailable):
        mcp_client.catalog("finchat")


# --- the refusal that matters ------------------------------------------------
def test_an_unreachable_tool_server_refuses_instead_of_answering(monkeypatch):
    """The single most important behaviour on this surface.

    A model asked for a balance will happily produce a plausible number. If the tool
    server is down, the only safe answer is that there is no answer — and the model must
    not be called at all, so there is nothing to leak into a reply by accident.
    """
    called = []
    monkeypatch.setattr(mcp_client, "catalog",
                        lambda *a, **k: (_ for _ in ()).throw(
                            mcp_client.MCPUnavailable("finchat: ConnectError")))
    monkeypatch.setattr(mcp_endpoint.passthrough, "generate",
                        lambda *a, **k: called.append(1) or _model_reply("hi"))

    out = mcp_endpoint.mcp_chat(_req())

    assert out["outcome"] == "tools_unavailable"
    assert not called, "the model was called even though no tools were reachable"
    assert "does not fall back" in out["reply"]


def test_a_server_that_dies_mid_loop_withholds_the_partial_answer(monkeypatch):
    monkeypatch.setattr(mcp_client, "catalog", lambda *a, **k: _catalog())
    monkeypatch.setattr(mcp_endpoint.passthrough, "generate", lambda *a, **k: _model_reply(
        calls=[("get_account_balance", {"account_id": "acct-001"})]))
    monkeypatch.setattr(mcp_client, "call_tools",
                        lambda *a, **k: (_ for _ in ()).throw(
                            mcp_client.MCPUnavailable("finchat: ReadTimeout")))

    out = mcp_endpoint.mcp_chat(_req())
    assert out["outcome"] == "tools_unavailable"
    assert "withheld" in out["reply"]


# --- governance around the loop ----------------------------------------------
def test_the_budget_is_charged_for_every_turn_not_just_the_first(monkeypatch, tmp_path):
    """A tool-calling answer is N model calls. Charging one is how spend escapes."""
    monkeypatch.setattr(mcp_client, "catalog", lambda *a, **k: _catalog())
    replies = iter([
        _model_reply(calls=[("get_account_balance", {"account_id": "acct-001"})],
                     in_tok=500, out_tok=30),
        _model_reply("The balance is 2,490.81 USD.", in_tok=900, out_tok=40),
    ])
    monkeypatch.setattr(mcp_endpoint.passthrough, "generate", lambda *a, **k: next(replies))
    monkeypatch.setattr(mcp_client, "call_tools", lambda *a, **k: ['{"balance": 2490.81}'])

    out = mcp_endpoint.mcp_chat(_req())

    assert out["outcome"] == "ok"
    assert out["turns"] == 2
    assert out["usage"] == {"input_tokens": 1400, "output_tokens": 70}
    # 1470 charged, not the 530 of the first turn.
    assert budget._local_usage[budget._doc_key("demo-manager")] == 1470


def test_tool_results_are_screened_before_they_reach_the_model(monkeypatch):
    """`passthrough.py` skips functionResponse payloads when screening an agent's own
    request, and documents why: that output came from the platform's own governed data.
    Neither half holds for a server this gateway does not own."""
    monkeypatch.setattr(mcp_client, "catalog", lambda *a, **k: _catalog())
    seen = []
    replies = iter([
        _model_reply(calls=[("get_account_balance", {"account_id": "acct-001"})]),
        _model_reply("Reported."),
    ])

    def _generate(body, model, **kw):
        seen.append(body)
        return next(replies)

    monkeypatch.setattr(mcp_endpoint.passthrough, "generate", _generate)
    monkeypatch.setattr(mcp_client, "call_tools",
                        lambda *a, **k: ["contact the customer at jane.doe@example.com"])

    out = mcp_endpoint.mcp_chat(_req())

    assert out["outcome"] == "ok"
    assert "EMAIL_ADDRESS" in out["pii"]["tool_findings"]
    assert out["tool_calls"][0]["withheld"] is True
    # The address must not appear anywhere in what the model was subsequently sent.
    assert "jane.doe@example.com" not in str(seen[-1])


def test_clean_tool_output_reaches_the_model_unchanged(monkeypatch):
    """The other half of the screen: it must not be a filter that eats real answers."""
    monkeypatch.setattr(mcp_client, "catalog", lambda *a, **k: _catalog())
    seen = []
    replies = iter([
        _model_reply(calls=[("get_account_balance", {"account_id": "acct-001"})]),
        _model_reply("2,490.81 USD."),
    ])
    monkeypatch.setattr(mcp_endpoint.passthrough, "generate",
                        lambda body, model, **kw: seen.append(body) or next(replies))
    monkeypatch.setattr(mcp_client, "call_tools",
                        lambda *a, **k: ['{"balance": 2490.81, "currency": "USD"}'])

    out = mcp_endpoint.mcp_chat(_req())
    assert out["pii"]["tool_findings"] == []
    assert out["tool_calls"][0]["withheld"] is False
    assert "2490.81" in str(seen[-1])


def test_the_servers_own_instructions_are_given_to_the_model(monkeypatch):
    """MCP's `instructions` field is the only place the protocol lets a server constrain
    a client's model. A client that drops it produces answers the owning platform cannot
    stand behind — which is precisely the FinChat refusal policy in this case."""
    monkeypatch.setattr(mcp_client, "catalog",
                        lambda *a, **k: _catalog("Never state that an account action has been taken."))
    seen = []
    monkeypatch.setattr(mcp_endpoint.passthrough, "generate",
                        lambda body, model, **kw: seen.append(body) or _model_reply("ok"))

    mcp_endpoint.mcp_chat(_req())
    system = seen[0]["systemInstruction"]["parts"][0]["text"]
    assert "Never state that an account action has been taken." in system


def test_the_audit_row_names_the_tools_the_model_called(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_client, "catalog", lambda *a, **k: _catalog())
    replies = iter([
        _model_reply(calls=[("get_account_balance", {"account_id": "acct-001"})]),
        _model_reply("2,490.81 USD."),
    ])
    monkeypatch.setattr(mcp_endpoint.passthrough, "generate", lambda *a, **k: next(replies))
    monkeypatch.setattr(mcp_client, "call_tools", lambda *a, **k: ['{"balance": 2490.81}'])

    mcp_endpoint.mcp_chat(_req())
    row = [e for e in _events(tmp_path) if e["outcome"] == "ok"][-1]
    assert row["surface"] == "mcp"
    assert row["mcp_server"] == "finchat"
    assert row["tools_called"] == ["get_account_balance"]
    assert row["turns"] == 2


def test_every_audit_field_this_surface_emits_exists_in_the_bigquery_schema(tmp_path,
                                                                            monkeypatch):
    """BigQuery inserts use ignore_unknown_values, so a column that does not exist is
    discarded in silence. That cost a day of attribution data once already."""
    from pathlib import Path

    monkeypatch.setattr(mcp_client, "catalog", lambda *a, **k: _catalog())
    replies = iter([
        _model_reply(calls=[("get_account_balance", {"account_id": "acct-001"})]),
        _model_reply("done"),
    ])
    monkeypatch.setattr(mcp_endpoint.passthrough, "generate", lambda *a, **k: next(replies))
    monkeypatch.setattr(mcp_client, "call_tools", lambda *a, **k: ["{}"])
    mcp_endpoint.mcp_chat(_req())

    ddl = (Path(__file__).resolve().parent.parent / "schema" / "requests.sql").read_text(
        encoding="utf-8")
    emitted = set(_events(tmp_path)[-1]) - {"ts"}
    # Fields present in the original /v1/chat table, before this file started tracking DDL.
    original = {"user_id", "prompt_chars", "outcome", "tier", "model", "input_tokens",
                "output_tokens", "pii_engine", "pii_findings", "pii_prompt_redacted",
                "pii_response_findings", "tokens_used", "daily_limit"}
    missing = sorted(f for f in emitted - original if f not in ddl)
    assert not missing, f"audit fields with no column: {missing}"


def test_running_out_of_tool_turns_says_so_instead_of_returning_a_half_answer(monkeypatch):
    monkeypatch.setattr(mcp_client, "catalog", lambda *a, **k: _catalog())
    monkeypatch.setattr(mcp_endpoint.passthrough, "generate", lambda *a, **k: _model_reply(
        calls=[("get_account_balance", {"account_id": "acct-001"})]))
    monkeypatch.setattr(mcp_client, "call_tools", lambda *a, **k: ["{}"])

    out = mcp_endpoint.mcp_chat(_req(max_tool_turns=2))
    assert out["turns"] == 2
    assert "tool-call limit" in out["reply"]


def test_an_unprovisioned_identity_never_reaches_the_tools(monkeypatch):
    reached = []
    monkeypatch.setattr(mcp_client, "catalog", lambda *a, **k: reached.append(1) or _catalog())
    out = mcp_endpoint.mcp_chat(_req(user_id="stranger@example.com"))
    assert out["outcome"] == "unauthorized"
    assert not reached


def test_pii_in_the_question_is_blocked_before_any_tool_is_discovered(monkeypatch):
    """Order matters: discovery is a network call to the tool server carrying nothing,
    but a blocked prompt should cost nothing and touch nothing."""
    reached = []
    monkeypatch.setattr(mcp_client, "catalog", lambda *a, **k: reached.append(1) or _catalog())
    out = mcp_endpoint.mcp_chat(_req(message="Check the account for SSN 123-45-6789"))
    assert out["outcome"] == "pii_blocked"
    assert not reached


def test_a_refusal_from_a_tool_is_passed_to_the_model_as_the_answer(monkeypatch):
    """A tool that refuses is not a transport failure. FinChat's refusal policy is the
    bank's policy; turning it into an error would invite a retry or a guess."""
    monkeypatch.setattr(mcp_client, "catalog", lambda *a, **k: _catalog())
    seen = []
    replies = iter([
        _model_reply(calls=[("get_account_balance", {"account_id": "nope"})]),
        _model_reply("I can't advise on your situation."),
    ])
    monkeypatch.setattr(mcp_endpoint.passthrough, "generate",
                        lambda body, model, **kw: seen.append(body) or next(replies))
    refusal = "Identifying details aren't available on this surface by design."
    monkeypatch.setattr(mcp_client, "call_tools", lambda *a, **k: [refusal])

    out = mcp_endpoint.mcp_chat(_req())
    assert out["outcome"] == "ok"
    assert refusal in str(seen[-1])
