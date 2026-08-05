"""Workload resolution for service-to-service traffic.

`personas.py` answers "which human is this, and what are they entitled to". This answers
the same question for a **registered agent or machine call site**: which workload is this,
what tier may it use, and what is its daily token allowance.

The two are deliberately separate. A human persona carries conversation history, memories
and a context blurb; a workload carries none of that — it is a stateless governed
completion attributed to an agent id that exists in the calling platform's agent registry.
Collapsing them would mean either giving agents conversational memory they should not have,
or stripping humans of context they need.

Registration is deployment config, not code:

    WORKLOAD_BUDGETS="tool_calling_agent:200000;evaluation:80000;classification:40000"
    WORKLOAD_TIERS="classification:standard;evaluation:standard"

An unregistered workload class is rejected — same posture as an unprovisioned human. The
alternative, defaulting unknown callers to a permissive tier, is how a gateway becomes a
proxy.
"""
import os

from .settings import CONFIG

# Default allowances by workload class. Deliberately coarse: the point is that spend is
# bounded and attributable per class, not that the numbers are tuned. Real limits come
# from WORKLOAD_BUDGETS at deploy time.
DEFAULT_WORKLOAD_BUDGETS = {
    "tool_calling_agent": 200_000,   # customer-facing agents; the heaviest consumer
    "grounded_generation": 100_000,  # RAG / semantics answering
    "reasoning": 80_000,             # planners, proposers
    "evaluation": 80_000,            # LLM-judge; bounded so scoring can't outspend serving
    "classification": 40_000,        # intent routing; cheapest, highest volume
}

# Classes clamped to a specific tier regardless of what the caller asks for. Routing a
# one-word intent classification to a premium model is the single most common way agent
# platforms waste money, so it is prevented here rather than trusted to the caller.
DEFAULT_WORKLOAD_TIERS = {
    "classification": "standard",
    "evaluation": "standard",
}


def _parse_pairs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for group in (raw or "").split(";"):
        if ":" not in group:
            continue
        key, value = group.split(":", 1)
        if key.strip() and value.strip():
            out[key.strip()] = value.strip()
    return out


def _budgets() -> dict[str, int]:
    budgets = dict(DEFAULT_WORKLOAD_BUDGETS)
    for key, value in _parse_pairs(os.environ.get("WORKLOAD_BUDGETS", "")).items():
        try:
            budgets[key] = int(value)
        except ValueError:
            continue  # a malformed override must not silently widen a budget
    return budgets


def _tier_clamps() -> dict[str, str]:
    clamps = dict(DEFAULT_WORKLOAD_TIERS)
    tiers = CONFIG["tiers"]
    for key, value in _parse_pairs(os.environ.get("WORKLOAD_TIERS", "")).items():
        if value in tiers:
            clamps[key] = value
    return clamps


def resolve(workload_class: str) -> dict | None:
    """Returns {name, daily_tokens, clamp_tier} or None for an unregistered class."""
    budgets = _budgets()
    if workload_class not in budgets:
        return None
    return {
        "name": workload_class,
        "daily_tokens": budgets[workload_class],
        "clamp_tier": _tier_clamps().get(workload_class),
    }


def known_classes() -> list[str]:
    return sorted(_budgets())


# Daily allowance for a human, when a call is made ON BEHALF OF one. Deliberately
# separate from the workload-class limits: those bound what an autonomous agent may
# spend, this bounds what a person may cause to be spent. Override per deployment with
# USER_BUDGETS="analyst:60000;auditor:20000".
DEFAULT_USER_BUDGET = 50_000


def user_budget(persona: str | None = None) -> int:
    budgets = _parse_pairs(os.environ.get("USER_BUDGETS", ""))
    try:
        return int(budgets[persona]) if persona in budgets else DEFAULT_USER_BUDGET
    except ValueError:
        return DEFAULT_USER_BUDGET


def budget_key(agent_id: str, workload_class: str, on_behalf_of: str | None = None) -> str:
    """Who the spend is charged to.

    Two cases, and conflating them makes both useless:

      * **User-initiated** (`on_behalf_of` set) — an analyst asking a question. Charged to
        the PERSON, because the answer to "what is my remaining budget" has to come from
        somewhere, and because a shared agent id tells you nothing about who spent it.
        `analyst_intent_router` is used by every analyst; billing them collectively means
        one heavy user silently consumes everyone else's allowance.
      * **Autonomous** (no `on_behalf_of`) — the steward, the nightly judge. No human
        caused it, so it is charged to the agent. Attributing it to whoever happened to
        trigger a deploy would be fiction.

    Charging per workload class was rejected for the same reason in both cases: it lets
    one noisy consumer exhaust every other consumer doing the same kind of work.
    """
    if on_behalf_of:
        return f"user:{on_behalf_of.strip().lower()}"
    return f"agent:{agent_id or 'unattributed'}"
