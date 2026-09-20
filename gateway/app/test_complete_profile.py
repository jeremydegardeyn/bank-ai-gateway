"""Tests for the /v1/complete classification profile.

The failure these pin was found live on 2026-09-19: the `classification` class is clamped
to gemini-2.5-flash, a thinking model, and its reasoning is billed against the caller's
max_output_tokens before the answer starts. FinChat's conversation-safety classifier got
7 tokens of a 150-token JSON verdict back at max_output_tokens=256 — every time — and a
truncated verdict ~1 in 15 times at 1024. Each test here fails if the corresponding half
of the fix is removed.

Runs fully offline: the provider is the one seam and it is replaced with a recorder.
"""
import json

import pytest

from . import audit, main, workloads
from .guards import budget, pii
from .providers import PROVIDERS


class Recorder:
    """Stands in for the Gemini provider: records what the gateway asked for, returns a
    canned reply. `text` can be a callable receiving the profile so a test can answer
    differently for JSON mode."""

    def __init__(self, text='{"signals":{"jailbreak_probe":0.9},"agent_refused":true}',
                 out_tok=40, thoughts=0, finish="STOP"):
        self.text, self.out_tok, self.thoughts, self.finish = text, out_tok, thoughts, finish
        self.calls = []

    def __call__(self, prompt, model, max_output_tokens, profile=None):
        self.calls.append({"prompt": prompt, "model": model,
                           "max_output_tokens": max_output_tokens, "profile": profile})
        text = self.text(profile) if callable(self.text) else self.text
        return {"text": text, "input_tokens": 100, "output_tokens": self.out_tok,
                "thoughts_tokens": self.thoughts, "finish_reason": self.finish,
                "model": model, "model_version": "gemini-2.5-flash-001"}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "LOCAL_AUDIT_LOG", tmp_path / "audit.jsonl")
    monkeypatch.setattr(audit, "GCP_PROJECT", "")
    monkeypatch.delenv("WORKLOAD_PROFILES", raising=False)
    monkeypatch.delenv("WORKLOAD_TIERS", raising=False)
    budget._local_usage.clear()


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()
    monkeypatch.setitem(PROVIDERS, "gemini", rec)
    monkeypatch.setitem(PROVIDERS, "claude", rec)
    return rec


def _events(tmp_path):
    log = tmp_path / "audit.jsonl"
    return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] \
        if log.exists() else []


def _complete(**kw):
    base = {"agent_id": "conversation_safety_classifier", "workload_class": "classification",
            "prompt": "Rate this turn. Return ONLY minified JSON.", "max_output_tokens": 256}
    base.update(kw)
    return main.complete(main.CompleteRequest(**base))


# --- the profile reaches the provider ----------------------------------------------------
def test_classification_class_turns_reasoning_off(recorder):
    """The half of the fix that stops the truncation: thinking budget 0 for the class,
    regardless of what the caller passed. Without it the model reasons first and the
    answer is whatever is left of max_output_tokens."""
    out = _complete()
    assert out["outcome"] == "ok"
    profile = recorder.calls[0]["profile"]
    assert profile["thinking_budget"] == 0
    assert profile["temperature"] == 0.0
    assert out["profile"] == "classification"


def test_classification_class_does_not_force_json_mode(recorder):
    """The same class serves FinChat's one-word intent router and its bare-array KB
    reranker. JSON mode is the caller's statement about its output, not the class's."""
    _complete()
    assert recorder.calls[0]["profile"]["response_mime_type"] is None


def test_response_format_json_switches_the_model_to_json_mode(recorder):
    out = _complete(response_format="json")
    profile = recorder.calls[0]["profile"]
    assert profile["response_mime_type"] == "application/json"
    assert profile["thinking_budget"] == 0
    assert out["profile"] == "json"


def test_response_format_json_applies_to_any_class(recorder):
    """A `reasoning` call that asks for JSON gets JSON mode too; the flag is not gated on
    the class being one that already has a profile."""
    _complete(workload_class="reasoning", response_format="json")
    assert recorder.calls[0]["profile"]["response_mime_type"] == "application/json"


def test_unprofiled_class_runs_with_model_defaults(recorder):
    out = _complete(workload_class="reasoning")
    assert recorder.calls[0]["profile"] is None
    assert out["profile"] is None


def test_caller_temperature_overrides_the_profile(recorder):
    """FinChat's gateway_llm has been sending `temperature` on every call; until now the
    field was silently dropped. It now lands, and wins over the profile's value."""
    _complete(temperature=0.7)
    assert recorder.calls[0]["profile"]["temperature"] == 0.7
    assert recorder.calls[0]["profile"]["thinking_budget"] == 0  # the rest still applies


def test_unknown_response_format_does_not_disable_the_class_profile(recorder):
    """A typo must not silently turn a classifier back into a thinking call."""
    _complete(response_format="jsno")
    assert recorder.calls[0]["profile"]["thinking_budget"] == 0
    assert recorder.calls[0]["profile"]["response_mime_type"] is None


def test_workload_profiles_env_overrides(monkeypatch):
    monkeypatch.setenv("WORKLOAD_PROFILES", "evaluation:json;reasoning:nonsense")
    assert workloads.resolve("evaluation")["profile"] == "json"
    assert workloads.resolve("reasoning")["profile"] is None  # unknown name ignored
    assert workloads.resolve("classification")["profile"] == "classification"


# --- usage on the payload ----------------------------------------------------------------
def test_output_tokens_are_on_the_payload_top_level(recorder):
    """FinChat read `output_tokens` off the payload and got None: it lived only under
    `usage`. A budget-exhaustion signal nobody can find is not a signal."""
    out = _complete()
    assert out["output_tokens"] == 40
    assert out["input_tokens"] == 100
    assert out["usage"]["output_tokens"] == 40


def test_truncation_is_visible_as_finish_reason_and_thoughts(monkeypatch):
    """What the live failure looks like on the wire: 7 tokens of JSON, 249 of reasoning,
    finish MAX_TOKENS. The caller can now see WHERE the budget went."""
    rec = Recorder(text='{"signals":{"jailbreak_', out_tok=7, thoughts=249, finish="MAX_TOKENS")
    monkeypatch.setitem(PROVIDERS, "gemini", rec)
    out = _complete()
    assert out["finish_reason"] == "MAX_TOKENS"
    assert out["thoughts_tokens"] == 249
    assert out["output_tokens"] == 7


def test_reasoning_tokens_are_charged_to_the_budget(monkeypatch, tmp_path):
    """Vertex bills thinking as output. Charging only candidates_token_count under-counted
    a thinking call by the whole reasoning trace."""
    rec = Recorder(out_tok=7, thoughts=249)
    monkeypatch.setitem(PROVIDERS, "gemini", rec)
    out = _complete()
    assert out["budget"]["used"] == 100 + 7 + 249
    ev = _events(tmp_path)[-1]
    assert ev["thoughts_tokens"] == 249 and ev["profile"] == "classification"


# --- response PII screening keeps JSON parseable -----------------------------------------
VERDICT_WITH_PII = ('{"signals":{"identity_probe":0.8,"answer_policy_breach":0.9},'
                    '"agent_refused":false,'
                    '"rationale":"The answer disclosed jane.doe@example.com and SSN 123-45-6789."}')


def test_json_response_is_redacted_by_value_and_stays_parseable(monkeypatch, tmp_path):
    """The flat redaction would have produced text FinChat logged as `parse:redacted` and
    counted as an unscreened turn. Values are rewritten, structure is kept, the audit row
    says which redaction ran."""
    monkeypatch.setitem(PROVIDERS, "gemini", Recorder(text=VERDICT_WITH_PII))
    out = _complete(response_format="json")
    doc = json.loads(out["text"])                       # still a document
    assert doc["signals"] == {"identity_probe": 0.8, "answer_policy_breach": 0.9}
    assert doc["agent_refused"] is False
    assert "jane.doe@example.com" not in out["text"]
    assert "123-45-6789" not in out["text"]
    assert "[EMAIL_ADDRESS]" in doc["rationale"] and "[US_SOCIAL_SECURITY_NUMBER]" in doc["rationale"]
    assert sorted(out["pii"]["response_findings"]) == ["EMAIL_ADDRESS", "US_SOCIAL_SECURITY_NUMBER"]
    assert out["pii"]["response_redaction"] == "json"
    assert _events(tmp_path)[-1]["pii_response_redaction"] == "json"


def test_json_shaped_reply_is_kept_parseable_even_without_the_flag(monkeypatch):
    """FinChat's classifier does not (yet) send response_format; its verdict is JSON all
    the same. The structured pass keys on the document, not on the flag."""
    monkeypatch.setitem(PROVIDERS, "gemini", Recorder(text=VERDICT_WITH_PII))
    out = _complete()
    assert json.loads(out["text"])["rationale"].count("[") == 2
    assert out["pii"]["response_redaction"] == "json"


def test_unparseable_reply_falls_back_to_flat_redaction(monkeypatch):
    """A truncated document is already unusable; the flat redaction still runs so PII
    never leaves in the fragment."""
    monkeypatch.setitem(PROVIDERS, "gemini",
                        Recorder(text='{"rationale":"call 555-123-4567 about'))
    out = _complete(response_format="json")
    assert "555-123-4567" not in out["text"]
    assert out["pii"]["response_redaction"] == "flat"


def test_clean_json_reply_is_untouched(recorder):
    out = _complete(response_format="json")
    assert out["text"] == recorder.text
    assert out["pii"]["response_redaction"] is None
    assert out["pii"]["response_findings"] == []


def test_redact_json_leaves_keys_numbers_and_nesting_alone():
    v = pii.screen('{"a":{"b":[1,"x@y.com",true,null]},"c":"HB-12345678"}', kind="response")
    out = json.loads(pii.redact_json('{"a":{"b":[1,"x@y.com",true,null]},"c":"HB-12345678"}', v))
    assert out == {"a": {"b": [1, "[EMAIL_ADDRESS]", True, None]}, "c": "[BANK_INTERNAL_ACCOUNT]"}


# --- the actual FinChat prompt, offline --------------------------------------------------
def test_finchat_classifier_prompt_round_trips_through_parse_verdict(recorder):
    """The offline half of the live check: the gateway does not reshape a well-formed
    verdict on its way back to `safety_signals.parse_verdict`. The live half (10/10 parses
    at max_output_tokens=256 against Vertex) is in the PR description, not CI."""
    pytest.importorskip("safety_signals", reason="FinChat repo not on sys.path")
    import safety_signals as ss
    out = _complete(prompt=ss.CLASSIFIER_PROMPT.format(question="hi", answer="hello"))
    assert ss.parse_verdict(out["text"]) is not None
