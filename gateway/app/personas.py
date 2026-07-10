"""Persona resolution: verified email → role entitlements.

The email→persona mapping is deployment config, not code:
  PERSONA_EMAILS="manager:a@corp.com;analyst:b@corp.com,c@corp.com;auditor:d@gmail.com"

An email with no mapping gets None — the caller rejects the request, which is
the enterprise-correct behavior (provisioned users only). In local dev (no
PERSONA_EMAILS set), the legacy demo-* user ids map onto personas so the mock
flow still works without sign-in."""
import os

from .settings import CONFIG

_DEV_USERS = {
    "demo-manager": "manager",
    "demo-analyst": "analyst",
    "demo-intern": "auditor",
}


def _parse_mapping() -> dict[str, str]:
    raw = os.environ.get("PERSONA_EMAILS", "")
    mapping: dict[str, str] = {}
    for group in raw.split(";"):
        if ":" not in group:
            continue
        persona, emails = group.split(":", 1)
        for email in emails.split(","):
            if email.strip():
                mapping[email.strip().lower()] = persona.strip()
    return mapping


EMAIL_TO_PERSONA = _parse_mapping()


def resolve(user_id: str) -> dict | None:
    """Returns {name, label, daily_tokens, allowed_tiers, context} or None."""
    user_id = (user_id or "").lower()
    name = EMAIL_TO_PERSONA.get(user_id)
    if name is None and not EMAIL_TO_PERSONA:
        name = _DEV_USERS.get(user_id)  # local dev fallback only
    cfg = CONFIG.get("personas", {}).get(name)
    if not cfg:
        return None
    return {"name": name, **cfg}
