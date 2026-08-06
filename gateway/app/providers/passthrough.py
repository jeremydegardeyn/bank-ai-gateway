"""Structured passthrough to Vertex `generateContent`.

`/v1/complete` flattens a request to a prompt string, which is fine for classification,
judging and single-shot generation — and useless for a tool-calling agent, because a
string cannot carry function declarations, a `functionCall` the model emitted, or the
`functionResponse` the runtime sends back. Flattening an agent turn would silently strip
its tools.

So agent traffic needs the request forwarded structurally: the gateway inspects and
governs the body, then passes it through intact and returns the provider's response
unchanged. The governance happens *around* the payload rather than by rewriting it.

Mock mode (no GCP_PROJECT) returns a well-formed response so the whole agent path runs
offline, matching the behaviour of the other providers.
"""
import json
import urllib.error
import urllib.request

from ..settings import GCP_PROJECT, GCP_REGION


class PassthroughError(RuntimeError):
    """Vertex refused, and said why. Carries the message, not just the status."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"vertex {status}: {detail}" if detail else f"vertex {status}")
        self.status = status
        self.detail = detail


def _text_parts(contents: list) -> list[str]:
    """Every user-authored text string in the request, for PII screening.

    Deliberately skips `functionResponse` payloads: those are tool output the platform
    itself produced from governed data, and screening them would flag the account data
    the agent was legitimately asked to retrieve. Screening the model's *reply* is where
    that risk is actually caught — see the response screen in main.py.
    """
    out = []
    for c in contents or []:
        for p in c.get("parts", []) or []:
            t = p.get("text")
            if isinstance(t, str) and t.strip():
                out.append(t)
    return out


def _redact_text_parts(contents: list, redactor) -> list:
    """Apply a redaction function to text parts in place, preserving structure."""
    for c in contents or []:
        for p in c.get("parts", []) or []:
            if isinstance(p.get("text"), str) and p["text"].strip():
                p["text"] = redactor(p["text"])
    return contents


def _response_text(payload: dict) -> str:
    parts = []
    for cand in payload.get("candidates", []) or []:
        for p in (cand.get("content") or {}).get("parts", []) or []:
            if isinstance(p.get("text"), str):
                parts.append(p["text"])
    return "\n".join(parts)


def _mock(model: str, body: dict) -> dict:
    prompt_chars = sum(len(t) for t in _text_parts(body.get("contents", [])))
    return {
        "candidates": [{
            "content": {"role": "model", "parts": [
                {"text": f"[mock:{model}] Simulated agent reply. "
                         "Set GCP_PROJECT to route to Vertex AI."}]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": prompt_chars // 4,
                          "candidatesTokenCount": 24},
        "modelVersion": None,
    }


def generate(body: dict, model: str, timeout: float = 120.0) -> dict:
    """Forward a generateContent body to Vertex and return the raw response payload."""
    if not GCP_PROJECT:
        return _mock(model, body)

    import google.auth
    import google.auth.transport.requests

    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())

    url = (f"https://{GCP_REGION}-aiplatform.googleapis.com/v1/projects/{GCP_PROJECT}"
           f"/locations/{GCP_REGION}/publishers/google/models/{model}:generateContent")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {creds.token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # Read the body. urllib discards it otherwise, and the body is the only part that
        # says WHY — "Invalid value at 'system_instruction'" versus a quota 429 are the
        # same HTTPError to a caller that only logs the exception type. Six agent turns
        # failed for a day behind exactly that.
        try:
            detail = json.loads(e.read())["error"]["message"][:300]
        except Exception:
            detail = ""
        raise PassthroughError(e.code, detail) from None


def usage(payload: dict) -> tuple[int, int]:
    u = payload.get("usageMetadata") or {}
    return int(u.get("promptTokenCount") or 0), int(u.get("candidatesTokenCount") or 0)
