"""Per-user daily token budgets. Firestore-backed on GCP; in-memory locally.
This is the 'excessive token spend' control: every request is charged against
the user's daily allowance, and requests over the cap are rejected."""
from collections import defaultdict
from datetime import date

from ..settings import CONFIG, FIRESTORE_ENABLED, GCP_PROJECT

_local_usage: dict[str, int] = defaultdict(int)


def _daily_limit(user_id: str) -> int:
    budgets = CONFIG["budgets"]
    return budgets.get("per_user_overrides", {}).get(user_id, budgets["default_daily_tokens"])


def _doc_key(user_id: str) -> str:
    return f"{user_id}_{date.today().isoformat()}"


def _get_usage(user_id: str) -> int:
    if FIRESTORE_ENABLED:
        from google.cloud import firestore
        db = firestore.Client(project=GCP_PROJECT)
        snap = db.collection("token_budgets").document(_doc_key(user_id)).get()
        return snap.get("tokens_used") if snap.exists else 0
    return _local_usage[_doc_key(user_id)]


def check(user_id: str) -> tuple[bool, int, int]:
    """Returns (allowed, used, limit)."""
    used, limit = _get_usage(user_id), _daily_limit(user_id)
    return used < limit, used, limit


def record(user_id: str, tokens: int) -> int:
    """Charge tokens against the user's daily budget; returns remaining."""
    key = _doc_key(user_id)
    if FIRESTORE_ENABLED:
        from google.cloud import firestore
        db = firestore.Client(project=GCP_PROJECT)
        db.collection("token_budgets").document(key).set(
            {"user_id": user_id, "date": date.today().isoformat(),
             "tokens_used": firestore.Increment(tokens)},
            merge=True,
        )
    else:
        _local_usage[key] += tokens
    return max(0, _daily_limit(user_id) - _get_usage(user_id))
