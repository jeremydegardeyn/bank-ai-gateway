"""Bank AI Gateway — the governance layer every regulated enterprise builds
no matter which model sits behind it.

Identity model: the UI service verifies the user's Google sign-in and forwards
the verified email as user_id. The gateway is private (IAM), so only trusted
callers reach it. Personas map verified emails to entitlements: daily token
budget, allowed tiers, and personalized context injected into prompts.

Request pipeline:
  1. Persona check   — unprovisioned identities are rejected
  2. Budget check    — persona's daily token allowance
  3. PII screen      — Model Armor + local detectors; block or redact
  4. Tier routing    — persona-clamped; cheap model unless the query earns more
  5. Model call      — with persona context prepended
  6. Response screen — PII check on the reply
  7. History + audit — Firestore chat history, BigQuery audit record
"""
from fastapi import FastAPI
from pydantic import BaseModel

from . import audit, history, personas, routing
from .guards import budget, pii
from .providers import PROVIDERS
from .settings import CONFIG

app = FastAPI(title="Bank AI Gateway")


class ChatRequest(BaseModel):
    user_id: str          # verified email (or demo-* id in local dev)
    message: str
    tier: str | None = None  # None = let the router decide


# Note: /healthz is intercepted by Google's frontend on run.app and never
# reaches the container — hence /health.
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/me/{user_id}")
def me(user_id: str):
    persona = personas.resolve(user_id)
    if persona is None:
        return {"provisioned": False}
    _, used, limit = budget.check(user_id, persona["daily_tokens"])
    return {
        "provisioned": True,
        "persona": persona["name"],
        "label": persona["label"],
        "allowed_tiers": persona["allowed_tiers"],
        "budget": {"used": used, "limit": limit, "remaining": max(0, limit - used)},
    }


@app.get("/v1/history/{user_id}")
def get_history(user_id: str):
    if personas.resolve(user_id) is None:
        return {"messages": []}
    return {"messages": history.fetch(user_id)}


@app.post("/v1/chat")
def chat(req: ChatRequest):
    base_event = {"user_id": req.user_id, "prompt_chars": len(req.message)}

    # 1. Persona — reject identities nobody provisioned
    persona = personas.resolve(req.user_id)
    if persona is None:
        audit.log_event({**base_event, "outcome": "unauthorized"})
        return {"outcome": "unauthorized",
                "reply": "This account is not provisioned for the AI platform. "
                         "Contact your administrator for access."}

    # 2. Budget
    allowed, used, limit = budget.check(req.user_id, persona["daily_tokens"])
    if not allowed:
        audit.log_event({**base_event, "outcome": "budget_exceeded",
                         "persona": persona["name"],
                         "tokens_used": used, "daily_limit": limit})
        return {"outcome": "budget_exceeded",
                "reply": f"Daily token budget exhausted ({used}/{limit}). "
                         "Contact your administrator to raise the limit.",
                "budget": {"used": used, "limit": limit, "remaining": 0}}

    # 3. PII screen on the prompt
    verdict = pii.screen(req.message, kind="prompt")
    pii_action = CONFIG["pii"]["action"]
    prompt = req.message
    if verdict.match:
        if pii_action == "block":
            audit.log_event({**base_event, "outcome": "pii_blocked",
                             "persona": persona["name"],
                             "pii_engine": verdict.engine, "pii_findings": verdict.findings})
            return {"outcome": "pii_blocked",
                    "reply": "This message was blocked: it appears to contain "
                             f"sensitive data ({', '.join(verdict.findings)}). "
                             "Remove customer PII and try again. This incident has been logged.",
                    "pii": {"engine": verdict.engine, "findings": verdict.findings}}
        prompt = verdict.redacted_text or prompt  # redact mode

    # 4. Route, clamped to the persona's entitlement
    tier = routing.choose_tier(prompt, req.tier)
    tier_clamped = False
    if tier not in persona["allowed_tiers"]:
        tier, tier_clamped = persona["allowed_tiers"][0], True
    tier_cfg = CONFIG["tiers"][tier]

    # 5. Model call with persona context prepended
    model_prompt = prompt
    if persona.get("context"):
        model_prompt = f"[Context about the user: {persona['context']}]\n\n{prompt}"
    try:
        result = PROVIDERS[tier_cfg["provider"]](
            model_prompt, tier_cfg["model"], tier_cfg["max_output_tokens"]
        )
    except Exception as exc:
        audit.log_event({**base_event, "outcome": "model_error", "tier": tier,
                         "persona": persona["name"],
                         "model": tier_cfg["model"], "error": type(exc).__name__})
        return {"outcome": "model_error", "tier": tier, "model": tier_cfg["model"],
                "reply": f"The {tier} tier model is currently unavailable "
                         f"({type(exc).__name__}). Try the other tier or retry later.",
                "error": type(exc).__name__}

    # 6. PII screen on the response
    response_findings: list[str] = []
    if CONFIG["pii"].get("screen_responses"):
        out_verdict = pii.screen(result["text"], kind="response")
        if out_verdict.match:
            response_findings = out_verdict.findings
            result["text"] = out_verdict.redacted_text or "[response withheld: sensitive data detected]"

    # 7. History (screened content only), budget charge, audit
    history.save(req.user_id, "user", prompt)
    history.save(req.user_id, "assistant", result["text"],
                 {"tier": tier, "model": result["model"]})
    total_tokens = result["input_tokens"] + result["output_tokens"]
    remaining = budget.record(req.user_id, total_tokens, persona["daily_tokens"])
    audit.log_event({
        **base_event, "outcome": "ok", "tier": tier, "model": result["model"],
        "persona": persona["name"],
        "input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"],
        "pii_prompt_redacted": verdict.match, "pii_response_findings": response_findings,
    })

    return {
        "outcome": "ok",
        "reply": result["text"],
        "tier": tier,
        "tier_clamped": tier_clamped,
        "model": result["model"],
        "persona": persona["label"],
        "usage": {"input_tokens": result["input_tokens"],
                  "output_tokens": result["output_tokens"]},
        "budget": {"used": limit - remaining, "limit": limit, "remaining": remaining},
        "pii": {"prompt_redacted": verdict.match,
                "prompt_findings": verdict.findings,
                "response_findings": response_findings},
    }
