"""Environment-driven settings. With no GCP env vars set, every dependency
falls back to a local implementation so the whole stack runs offline."""
import os
from pathlib import Path

import yaml

GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")

# Full resource name, e.g. projects/PROJ/locations/us-central1/templates/bank-pii-guard
MODEL_ARMOR_TEMPLATE = os.environ.get("MODEL_ARMOR_TEMPLATE", "")

BQ_DATASET = os.environ.get("BQ_DATASET", "")          # e.g. ai_gateway
BQ_TABLE = os.environ.get("BQ_TABLE", "requests")
FIRESTORE_ENABLED = bool(GCP_PROJECT) and os.environ.get("USE_FIRESTORE", "1") == "1"

# Claude on Vertex region — "global" is the recommended default.
CLAUDE_VERTEX_REGION = os.environ.get("CLAUDE_VERTEX_REGION", "global")

CONFIG_PATH = Path(os.environ.get("GATEWAY_CONFIG", Path(__file__).resolve().parents[2] / "config.yaml"))

LOCAL_AUDIT_LOG = Path(os.environ.get("LOCAL_AUDIT_LOG", Path(__file__).resolve().parents[2] / "logs" / "audit.jsonl"))


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


CONFIG = load_config()
