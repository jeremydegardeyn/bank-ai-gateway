"""Tier routing — the cost-governance half of the gateway. Simple queries go to
the cheap model; long or analytical queries earn the premium model. The UI can
also pin a tier explicitly."""
from .settings import CONFIG


def choose_tier(message: str, requested: str | None = None) -> str:
    tiers = CONFIG["tiers"]
    if requested in tiers:
        return requested
    rules = CONFIG["routing"]
    text = message.lower()
    if len(message) >= rules["premium_min_chars"]:
        return "premium"
    if any(kw in text for kw in rules["premium_keywords"]):
        return "premium"
    return "standard"
