"""Context assembly and compaction.

Each request's model prompt is built from layers, cheapest-to-evict last:
persona context → user-provided context → long-term memories → conversation
summary (from prior compactions) → recent messages → the new message.

Compaction: when a conversation's total tokens exceed the configured trigger,
everything except the newest messages is summarized by the standard-tier model
into the conversation's rolling summary, and durable facts about the user are
extracted into long-term memory — so the window shrinks but nothing important
is lost. The compaction call's tokens are charged to the user like any other."""
import json
import os
import re

from . import store
from .providers import PROVIDERS
from .settings import CONFIG


def _cfg() -> dict:
    cfg = dict(CONFIG.get("context", {}))
    # Env overrides make the compaction path testable without a 100k conversation.
    for key in ("history_max_tokens", "compact_trigger_tokens", "keep_recent_messages"):
        env = os.environ.get(f"CONTEXT_{key.upper()}")
        if env and env.isdigit():
            cfg[key] = int(env)
    return cfg


def recent_messages(email: str, conv: dict) -> list[dict]:
    """Messages after the last compaction point, newest-last, trimmed to the
    history token budget (walking backward so the newest always survive)."""
    cfg = _cfg()
    msgs = store.get_messages(email, conv["id"])
    cutoff = conv.get("summary_through_ts") or ""
    msgs = [m for m in msgs if m["ts"] > cutoff]
    budget, kept = cfg.get("history_max_tokens", 8000), []
    for m in reversed(msgs):
        budget -= store.est_tokens(m["text"])
        if budget < 0:
            break
        kept.append(m)
    return list(reversed(kept))


def build_prompt(persona: dict, email: str, conv: dict,
                 history: list[dict], new_message: str) -> str:
    cfg = _cfg()
    parts = []
    if persona.get("context"):
        parts.append(f"[Context about the user: {persona['context']}]")
    user_ctx = store.get_context(email)
    if user_ctx:
        parts.append(f"[Context the user provided about themselves: {user_ctx}]")
    memories = store.list_memories(email, cfg.get("memory_max_items", 20))
    if memories:
        facts = "\n".join(f"- {m['text']}" for m in memories)
        parts.append(f"[Long-term memory about this user from past conversations:\n{facts}]")
    if conv.get("summary"):
        parts.append(f"[Summary of the earlier part of this conversation:\n{conv['summary']}]")
    if history:
        transcript = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['text']}"
            for m in history)
        parts.append(f"Conversation so far:\n{transcript}")
    parts.append(f"User: {new_message}\nAssistant:")
    return "\n\n".join(parts)


_COMPACT_PROMPT = """You are a conversation compactor for an enterprise AI assistant.
Below is the earlier part of a conversation (possibly preceded by an existing summary).
Produce JSON with exactly two keys:
  "summary": a concise summary (under 300 words) preserving decisions, facts, and open threads so the conversation can continue seamlessly;
  "memories": a list of 0-5 short durable facts about the USER worth remembering across future conversations (their role, preferences, ongoing projects). Only include facts stated by the user; return [] if none.
Reply with JSON only.

{existing}

{transcript}"""


def maybe_compact(email: str, conv: dict) -> dict | None:
    """Compact if the conversation exceeds the trigger. Returns
    {tokens, memories_added} when compaction ran, else None."""
    cfg = _cfg()
    if int(conv.get("token_count", 0)) <= cfg.get("compact_trigger_tokens", 100000):
        return None
    keep = cfg.get("keep_recent_messages", 10)
    msgs = store.get_messages(email, conv["id"])
    cutoff = conv.get("summary_through_ts") or ""
    fresh = [m for m in msgs if m["ts"] > cutoff]
    older = fresh[:-keep] if len(fresh) > keep else []
    if not older:
        return None

    transcript = "\n".join(
        f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['text']}" for m in older)
    existing = (f"Existing summary of even earlier turns:\n{conv['summary']}"
                if conv.get("summary") else "")
    tier_cfg = CONFIG["tiers"]["standard"]
    result = PROVIDERS[tier_cfg["provider"]](
        _COMPACT_PROMPT.format(existing=existing, transcript=transcript),
        tier_cfg["model"], 1024)

    summary, memories = result["text"].strip(), []
    match = re.search(r"\{.*\}", result["text"], re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            summary = str(parsed.get("summary", summary))
            memories = [str(m) for m in parsed.get("memories", [])][:5]
        except Exception:
            pass  # keep raw text as the summary

    store.update_conversation(email, conv["id"], {
        "summary": summary[:4000],
        "summary_through_ts": older[-1]["ts"],
        # Reset the counter to roughly what remains in the live window.
        "token_count": sum(store.est_tokens(m["text"]) for m in fresh[-keep:])
                       + store.est_tokens(summary),
    })
    for m in memories:
        store.add_memory(email, m)
    return {"tokens": result["input_tokens"] + result["output_tokens"],
            "memories_added": len(memories)}
