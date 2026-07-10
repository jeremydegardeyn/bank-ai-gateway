"""Per-user persistence: profile context, long-term memories, conversations,
and messages. Firestore on GCP (named database, same as budgets); in-memory
locally. Layout:

  users/{email}                          {context}
  users/{email}/memories/{id}            {text, ts}
  users/{email}/conversations/{id}       {title, updated_at, token_count,
                                          summary, summary_through_ts}
  users/{email}/conversations/{id}/messages/{id}
                                         {role, text, ts, tier?, model?}
"""
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from .settings import FIRESTORE_DATABASE, FIRESTORE_ENABLED, GCP_PROJECT

_ctx: dict[str, str] = {}
_mem: dict[str, list] = defaultdict(list)
_convs: dict[str, dict] = defaultdict(dict)
_msgs: dict[tuple, list] = defaultdict(list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def est_tokens(text: str) -> int:
    return len(text) // 4 + 1


def _user_doc(email: str):
    from google.cloud import firestore
    db = firestore.Client(project=GCP_PROJECT, database=FIRESTORE_DATABASE)
    return db.collection("users").document(email)


def _fs(fn, fallback):
    """Run a Firestore op with in-memory fallback on any failure."""
    if FIRESTORE_ENABLED:
        try:
            return fn()
        except Exception:
            pass
    return fallback()


# ── User context ─────────────────────────────────────────────────────────────
def get_context(email: str) -> str:
    def fs():
        snap = _user_doc(email).get()
        return (snap.get("context") if snap.exists else "") or ""
    return _fs(fs, lambda: _ctx.get(email, ""))


def set_context(email: str, text: str) -> None:
    text = text[:2000]
    def fs():
        _user_doc(email).set({"context": text}, merge=True)
        return True
    if not _fs(fs, lambda: None):
        _ctx[email] = text


# ── Long-term memories ───────────────────────────────────────────────────────
def list_memories(email: str, limit: int = 50) -> list[dict]:
    def fs():
        snaps = (_user_doc(email).collection("memories")
                 .order_by("ts", direction="DESCENDING").limit(limit).get())
        return [{"id": s.id, **s.to_dict()} for s in snaps]
    return _fs(fs, lambda: list(reversed(_mem[email][-limit:])))


def add_memory(email: str, text: str) -> None:
    text = text.strip()[:500]
    if not text or any(m["text"] == text for m in list_memories(email)):
        return  # skip empties and exact duplicates
    entry = {"text": text, "ts": _now()}
    def fs():
        _user_doc(email).collection("memories").add(entry)
        return True
    if not _fs(fs, lambda: None):
        _mem[email].append({"id": uuid.uuid4().hex[:12], **entry})


def delete_memory(email: str, memory_id: str) -> None:
    def fs():
        _user_doc(email).collection("memories").document(memory_id).delete()
        return True
    if not _fs(fs, lambda: None):
        _mem[email] = [m for m in _mem[email] if m["id"] != memory_id]


# ── Conversations ────────────────────────────────────────────────────────────
def list_conversations(email: str, limit: int = 30) -> list[dict]:
    def fs():
        snaps = (_user_doc(email).collection("conversations")
                 .order_by("updated_at", direction="DESCENDING").limit(limit).get())
        return [{"id": s.id, **s.to_dict()} for s in snaps]
    return _fs(fs, lambda: sorted(
        [{"id": k, **v} for k, v in _convs[email].items()],
        key=lambda c: c["updated_at"], reverse=True)[:limit])


def create_conversation(email: str, title: str) -> str:
    meta = {"title": title[:60], "created_at": _now(), "updated_at": _now(),
            "token_count": 0, "summary": "", "summary_through_ts": ""}
    def fs():
        ref = _user_doc(email).collection("conversations").document()
        ref.set(meta)
        return ref.id
    result = _fs(fs, lambda: None)
    if result:
        return result
    cid = uuid.uuid4().hex[:12]
    _convs[email][cid] = meta
    return cid


def get_conversation(email: str, cid: str) -> dict | None:
    def fs():
        snap = _user_doc(email).collection("conversations").document(cid).get()
        return {"id": cid, **snap.to_dict()} if snap.exists else None
    return _fs(fs, lambda: ({"id": cid, **_convs[email][cid]}
                            if cid in _convs[email] else None))


def update_conversation(email: str, cid: str, fields: dict) -> None:
    def fs():
        _user_doc(email).collection("conversations").document(cid).set(fields, merge=True)
        return True
    if not _fs(fs, lambda: None):
        _convs[email].setdefault(cid, {}).update(fields)


# ── Messages ─────────────────────────────────────────────────────────────────
def add_message(email: str, cid: str, role: str, text: str,
                meta: dict | None = None) -> None:
    entry = {"role": role, "text": text, "ts": _now(), **(meta or {})}
    def fs():
        (_user_doc(email).collection("conversations").document(cid)
         .collection("messages").add(entry))
        return True
    if not _fs(fs, lambda: None):
        _msgs[(email, cid)].append(entry)
    conv = get_conversation(email, cid) or {}
    update_conversation(email, cid, {
        "updated_at": _now(),
        "token_count": int(conv.get("token_count", 0)) + est_tokens(text),
    })


def get_messages(email: str, cid: str, limit: int = 200) -> list[dict]:
    def fs():
        snaps = (_user_doc(email).collection("conversations").document(cid)
                 .collection("messages").order_by("ts", direction="DESCENDING")
                 .limit(limit).get())
        return [s.to_dict() for s in reversed(list(snaps))]
    return _fs(fs, lambda: _msgs[(email, cid)][-limit:])
