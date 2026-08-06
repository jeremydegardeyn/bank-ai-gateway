"""Audit logging: every request, its PII verdict, tier, and token usage.
BigQuery streaming inserts on GCP; local JSONL otherwise. The BigQuery table
feeds the Looker Studio spend/PII dashboard.

Note: insert_rows_json does NOT raise on row errors — it returns them. Rows
with list/dict values are JSON-serialized to fit STRING columns, and unknown
fields are ignored rather than rejecting the row."""
import json
from datetime import datetime, timezone

from .settings import BQ_DATASET, BQ_TABLE, GCP_PROJECT, LOCAL_AUDIT_LOG


def _bq_row(event: dict) -> dict:
    return {k: json.dumps(v) if isinstance(v, (list, dict)) else v
            for k, v in event.items()}


# Fields the pipeline emits that MUST exist in the table. ignore_unknown_values keeps a
# row from being rejected over a schema mismatch — which is right, an audit row is worth
# more than its completeness — but it also means a missing column is discarded in silence.
# That is exactly what happened: agent_id, workload_class, session_id and on_behalf_of
# were written into the void for a day because the table still had the /v1/chat schema,
# and nothing anywhere said so. Warn once per process so the drift is visible without
# spamming every request.
_EXPECTED_FIELDS = {
    "agent_id", "workload_class", "session_id", "on_behalf_of", "owner",
    "surface", "model_served", "tier_clamped",
}
_schema_checked = False


def _warn_on_schema_drift(client) -> None:
    global _schema_checked
    if _schema_checked:
        return
    _schema_checked = True
    try:
        table = client.get_table(f"{GCP_PROJECT}.{BQ_DATASET}.{BQ_TABLE}")
        have = {f.name for f in table.schema}
        missing = sorted(_EXPECTED_FIELDS - have)
        if missing:
            print(f"audit: WARNING — {BQ_TABLE} is missing {missing}. Those fields are "
                  f"being DISCARDED on every insert (ignore_unknown_values). Attribution "
                  f"and unit economics will be empty until the table is altered.")
    except Exception:
        pass  # a schema probe must never break the audit path


def log_event(event: dict) -> None:
    event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    if GCP_PROJECT and BQ_DATASET:
        try:
            from google.cloud import bigquery
            client = bigquery.Client(project=GCP_PROJECT)
            _warn_on_schema_drift(client)
            errors = client.insert_rows_json(
                f"{GCP_PROJECT}.{BQ_DATASET}.{BQ_TABLE}",
                [_bq_row(event)],
                ignore_unknown_values=True,
            )
            if not errors:
                return
            print(f"audit: BigQuery rejected row: {errors}")
        except Exception as exc:
            print(f"audit: BigQuery insert failed: {exc}")
    # Never lose the audit record — fall back to the local log.
    LOCAL_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCAL_AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
