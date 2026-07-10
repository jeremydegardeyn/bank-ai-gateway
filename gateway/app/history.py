"""Per-user chat history. Firestore-backed on GCP (same named database as
budgets); in-memory locally. Only screened content is stored: prompts are saved
post-PII-redaction, and blocked prompts are never saved."""
from collections import defaultdict
from datetime import datetime, timezone

from .settings import FIRESTORE_DATABASE, FIRESTORE_ENABLED, GCP_PROJECT

_local: dict[str, list] = defaultdict(list)
_MAX_FETCH = 40


def _col(user_id: str):
    from google.cloud import firestore
    db = firestore.Client(project=GCP_PROJECT, database=FIRESTORE_DATABASE)
    return db.collection("chat_history").document(user_id).collection("messages")


def save(user_id: str, role: str, text: str, meta: dict | None = None) -> None:
    entry = {"role": role, "text": text,
             "ts": datetime.now(timezone.utc).isoformat(), **(meta or {})}
    if FIRESTORE_ENABLED:
        try:
            _col(user_id).add(entry)
            return
        except Exception:
            pass
    _local[user_id].append(entry)


def fetch(user_id: str, limit: int = _MAX_FETCH) -> list[dict]:
    if FIRESTORE_ENABLED:
        try:
            snaps = (_col(user_id).order_by("ts", direction="DESCENDING")
                     .limit(limit).get())
            return [s.to_dict() for s in reversed(list(snaps))]
        except Exception:
            pass
    return _local[user_id][-limit:]
