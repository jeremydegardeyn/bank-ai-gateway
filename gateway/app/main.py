"""Bank AI Gateway — the governance layer every regulated enterprise builds
no matter which model sits behind it.

Identity: the UI verifies Google sign-in and forwards the verified email as
user_id (the gateway is private, IAM-authenticated). Personas map emails to
entitlements. Each user has editable context, auto-extracted long-term
memories, and multiple conversations; large conversations are compacted into
a rolling summary + memories so the context window stays bounded.

Request pipeline:
  1. Persona check   — unprovisioned identities rejected
  2. Budget check    — persona's daily token allowance
  3. PII screen      — Model Armor + local detectors; block or redact
  4. Tier routing    — persona-clamped
  5. Model call      — persona context + user context + memories + summary + history
  6. Response screen — PII check on the reply
  7. Persist + audit — messages, compaction if due, budget charge, BigQuery
"""
from fastapi import FastAPI, Request
from pydantic import BaseModel

from . import audit, context, personas, routing, store
from .guards import budget, pii
from .providers import PROVIDERS
from .settings import CONFIG

app = FastAPI(title="Bank AI Gateway")


class ChatRequest(BaseModel):
    user_id: str              # verified email (or demo-* id in local dev)
    message: str
    tier: str | None = None
    conversation_id: str | None = None  # None = start a new conversation


# Note: /healthz is intercepted by Google's frontend on run.app — hence /health.
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
        "context": store.get_context(user_id),
        "budget": {"used": used, "limit": limit, "remaining": max(0, limit - used)},
    }


@app.put("/v1/context/{user_id}")
async def put_context(user_id: str, request: Request):
    if personas.resolve(user_id) is None:
        return {"ok": False}
    body = await request.json()
    store.set_context(user_id, str(body.get("context", "")))
    return {"ok": True}


@app.get("/v1/memories/{user_id}")
def get_memories(user_id: str):
    if personas.resolve(user_id) is None:
        return {"memories": []}
    return {"memories": store.list_memories(user_id)}


@app.delete("/v1/memories/{user_id}/{memory_id}")
def del_memory(user_id: str, memory_id: str):
    if personas.resolve(user_id) is None:
        return {"ok": False}
    store.delete_memory(user_id, memory_id)
    return {"ok": True}


@app.get("/v1/conversations/{user_id}")
def conversations(user_id: str):
    if personas.resolve(user_id) is None:
        return {"conversations": []}
    return {"conversations": [
        {"id": c["id"], "title": c.get("title", "Untitled"),
         "updated_at": c.get("updated_at", "")}
        for c in store.list_conversations(user_id)
    ]}


@app.get("/v1/conversations/{user_id}/{conv_id}")
def conversation(user_id: str, conv_id: str):
    if personas.resolve(user_id) is None:
        return {"messages": []}
    conv = store.get_conversation(user_id, conv_id)
    if conv is None:
        return {"messages": []}
    return {"id": conv_id, "title": conv.get("title", ""),
            "compacted": bool(conv.get("summary")),
            "messages": store.get_messages(user_id, conv_id)}


@app.post("/v1/chat")
def chat(req: ChatRequest):
    base_event = {"user_id": req.user_id, "prompt_chars": len(req.message)}

    # 1. Persona
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
    prompt = req.message
    if verdict.match:
        if CONFIG["pii"]["action"] == "block":
            audit.log_event({**base_event, "outcome": "pii_blocked",
                             "persona": persona["name"],
                             "pii_engine": verdict.engine, "pii_findings": verdict.findings})
            return {"outcome": "pii_blocked",
                    "reply": "This message was blocked: it appears to contain "
                             f"sensitive data ({', '.join(verdict.findings)}). "
                             "Remove customer PII and try again. This incident has been logged.",
                    "pii": {"engine": verdict.engine, "findings": verdict.findings}}
        prompt = verdict.redacted_text or prompt  # redact mode

    # Conversation: create on first message, load history for context
    conv_id = req.conversation_id
    conv = store.get_conversation(req.user_id, conv_id) if conv_id else None
    if conv is None:
        conv_id = store.create_conversation(req.user_id, prompt)
        conv = store.get_conversation(req.user_id, conv_id)
    history = context.recent_messages(req.user_id, conv)

    # 4. Route, clamped to the persona's entitlement
    tier = routing.choose_tier(prompt, req.tier)
    tier_clamped = False
    if tier not in persona["allowed_tiers"]:
        tier, tier_clamped = persona["allowed_tiers"][0], True
    tier_cfg = CONFIG["tiers"][tier]

    # 5. Model call with the assembled context
    model_prompt = context.build_prompt(persona, req.user_id, conv, history, prompt)
    try:
        result = PROVIDERS[tier_cfg["provider"]](
            model_prompt, tier_cfg["model"], tier_cfg["max_output_tokens"]
        )
    except Exception as exc:
        audit.log_event({**base_event, "outcome": "model_error", "tier": tier,
                         "persona": persona["name"],
                         "model": tier_cfg["model"], "error": type(exc).__name__})
        return {"outcome": "model_error", "tier": tier, "model": tier_cfg["model"],
                "conversation_id": conv_id,
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

    # 7. Persist (screened content only), compact if due, charge, audit
    store.add_message(req.user_id, conv_id, "user", prompt)
    store.add_message(req.user_id, conv_id, "assistant", result["text"],
                      {"tier": tier, "model": result["model"]})
    compaction = None
    conv = store.get_conversation(req.user_id, conv_id)
    if conv:
        try:
            compaction = context.maybe_compact(req.user_id, conv)
        except Exception:
            pass  # compaction is best-effort; never fail the chat for it

    total_tokens = result["input_tokens"] + result["output_tokens"]
    if compaction:
        total_tokens += compaction["tokens"]
    remaining = budget.record(req.user_id, total_tokens, persona["daily_tokens"])
    audit.log_event({
        **base_event, "outcome": "ok", "tier": tier, "model": result["model"],
        "persona": persona["name"],
        "input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"],
        "compaction_tokens": compaction["tokens"] if compaction else 0,
        "pii_prompt_redacted": verdict.match, "pii_response_findings": response_findings,
    })

    return {
        "outcome": "ok",
        "reply": result["text"],
        "conversation_id": conv_id,
        "tier": tier,
        "tier_clamped": tier_clamped,
        "model": result["model"],
        "persona": persona["label"],
        "compacted": bool(compaction),
        "memories_added": compaction["memories_added"] if compaction else 0,
        "usage": {"input_tokens": result["input_tokens"],
                  "output_tokens": result["output_tokens"]},
        "budget": {"used": limit - remaining, "limit": limit, "remaining": remaining},
        "pii": {"prompt_redacted": verdict.match,
                "prompt_findings": verdict.findings,
                "response_findings": response_findings},
    }
