"""Audit logging: every request, its PII verdict, tier, and token usage.
BigQuery streaming inserts on GCP; local JSONL otherwise. The BigQuery table
feeds the Looker Studio spend/PII dashboard."""
import json
from datetime import datetime, timezone

from .settings import BQ_DATASET, BQ_TABLE, GCP_PROJECT, LOCAL_AUDIT_LOG


def log_event(event: dict) -> None:
    event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    if GCP_PROJECT and BQ_DATASET:
        try:
            from google.cloud import bigquery
            client = bigquery.Client(project=GCP_PROJECT)
            client.insert_rows_json(f"{GCP_PROJECT}.{BQ_DATASET}.{BQ_TABLE}", [event])
            return
        except Exception:
            pass  # fall through to local log — never lose the audit record
    LOCAL_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCAL_AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
