"""Bank AI Gateway — the governance layer every bank builds no matter which
model sits behind it.

Request pipeline:
  1. Budget check   — reject if the user's daily token allowance is spent
  2. PII screen     — Model Armor (or local regex) on the prompt; block or redact
  3. Tier routing   — cheap model by default, premium when the query earns it
  4. Model call     — Gemini or Claude, both served inside the GCP tenancy
  5. Response screen — optional PII screen on the model's reply
  6. Audit          — BigQuery (or local JSONL) record of everything above
"""
from fastapi import FastAPI
from pydantic import BaseModel

from . import audit, routing
from .guards import budget, pii
from .providers import PROVIDERS
from .settings import CONFIG

app = FastAPI(title="Bank AI Gateway")


class ChatRequest(BaseModel):
    user_id: str
    message: str
    tier: str | None = None  # None = let the router decide


# Note: /healthz is intercepted by Google's frontend on run.app and never
# reaches the container — hence /health.
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/budget/{user_id}")
def get_budget(user_id: str):
    _, used, limit = budget.check(user_id)
    return {"user_id": user_id, "tokens_used": used, "daily_limit": limit,
            "remaining": max(0, limit - used)}


@app.post("/v1/chat")
def chat(req: ChatRequest):
    base_event = {"user_id": req.user_id, "prompt_chars": len(req.message)}

    # 1. Budget
    allowed, used, limit = budget.check(req.user_id)
    if not allowed:
        audit.log_event({**base_event, "outcome": "budget_exceeded",
                         "tokens_used": used, "daily_limit": limit})
        return {"outcome": "budget_exceeded",
                "reply": f"Daily token budget exhausted ({used}/{limit}). "
                         "Contact your administrator to raise the limit.",
                "budget": {"used": used, "limit": limit, "remaining": 0}}

    # 2. PII screen on the prompt
    verdict = pii.screen(req.message, kind="prompt")
    pii_action = CONFIG["pii"]["action"]
    prompt = req.message
    if verdict.match:
        if pii_action == "block":
            audit.log_event({**base_event, "outcome": "pii_blocked",
                             "pii_engine": verdict.engine, "pii_findings": verdict.findings})
            return {"outcome": "pii_blocked",
                    "reply": "This message was blocked: it appears to contain "
                             f"sensitive data ({', '.join(verdict.findings)}). "
                             "Remove customer PII and try again. This incident has been logged.",
                    "pii": {"engine": verdict.engine, "findings": verdict.findings}}
        prompt = verdict.redacted_text or prompt  # redact mode

    # 3 + 4. Route and call the model
    tier = routing.choose_tier(prompt, req.tier)
    tier_cfg = CONFIG["tiers"][tier]
    try:
        result = PROVIDERS[tier_cfg["provider"]](
            prompt, tier_cfg["model"], tier_cfg["max_output_tokens"]
        )
    except Exception as exc:
        audit.log_event({**base_event, "outcome": "model_error", "tier": tier,
                         "model": tier_cfg["model"], "error": type(exc).__name__})
        return {"outcome": "model_error", "tier": tier, "model": tier_cfg["model"],
                "reply": f"The {tier} tier model is currently unavailable "
                         f"({type(exc).__name__}). Try the other tier or retry later.",
                "error": type(exc).__name__}

    # 5. PII screen on the response
    response_findings: list[str] = []
    if CONFIG["pii"].get("screen_responses"):
        out_verdict = pii.screen(result["text"], kind="response")
        if out_verdict.match:
            response_findings = out_verdict.findings
            result["text"] = out_verdict.redacted_text or "[response withheld: sensitive data detected]"

    # 6. Budget charge + audit
    total_tokens = result["input_tokens"] + result["output_tokens"]
    remaining = budget.record(req.user_id, total_tokens)
    audit.log_event({
        **base_event, "outcome": "ok", "tier": tier, "model": result["model"],
        "input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"],
        "pii_prompt_redacted": verdict.match, "pii_response_findings": response_findings,
    })

    return {
        "outcome": "ok",
        "reply": result["text"],
        "tier": tier,
        "model": result["model"],
        "usage": {"input_tokens": result["input_tokens"],
                  "output_tokens": result["output_tokens"]},
        "budget": {"used": limit - remaining, "limit": limit, "remaining": remaining},
        "pii": {"prompt_redacted": verdict.match,
                "prompt_findings": verdict.findings,
                "response_findings": response_findings},
    }
