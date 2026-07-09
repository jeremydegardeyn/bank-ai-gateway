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


def log_event(event: dict) -> None:
    event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    if GCP_PROJECT and BQ_DATASET:
        try:
            from google.cloud import bigquery
            client = bigquery.Client(project=GCP_PROJECT)
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
