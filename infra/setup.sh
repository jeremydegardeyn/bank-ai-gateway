#!/usr/bin/env bash
# One-time GCP setup for the Bank AI Gateway. Idempotent-ish; safe to re-run.
# Usage: PROJECT_ID=my-project ./setup.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
BQ_DATASET="${BQ_DATASET:-ai_gateway}"
MA_TEMPLATE="${MA_TEMPLATE:-bank-pii-guard}"

gcloud config set project "$PROJECT_ID"

echo "── Enabling APIs…"
gcloud services enable \
  run.googleapis.com \
  aiplatform.googleapis.com \
  modelarmor.googleapis.com \
  firestore.googleapis.com \
  bigquery.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com

echo "── IAM for the Cloud Run runtime service account…"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
RUN_SA="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"
for role in roles/datastore.user roles/bigquery.dataEditor roles/aiplatform.user roles/modelarmor.user; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:$RUN_SA" --role "$role" --condition=None --quiet >/dev/null \
    || echo "could not grant $role — grant it manually if gateway calls fail"
done

echo "── Model Armor template (PII / sensitive-data screening)…"
# Model Armor is served from regional endpoints only — without this override,
# gcloud model-armor commands fail with a misleading PERMISSION_DENIED.
gcloud config set api_endpoint_overrides/modelarmor "https://modelarmor.$REGION.rep.googleapis.com/"
# Basic SDP config covers common PII infoTypes (SSN, credit card, etc.).
# For bank-internal account formats, create an SDP inspect template with a
# custom infoType and reference it here instead (advanced config).
gcloud model-armor templates create "$MA_TEMPLATE" \
  --location="$REGION" \
  --basic-config-filter-enforcement=enabled \
  || echo "template may already exist — check: gcloud model-armor templates list --location=$REGION"

echo "── Firestore (budgets)…"
gcloud firestore databases create --location="$REGION" --type=firestore-native \
  || echo "firestore database may already exist"

echo "── BigQuery (audit log)…"
bq --location=US mk --dataset "$PROJECT_ID:$BQ_DATASET" || true
bq mk --table "$PROJECT_ID:$BQ_DATASET.requests" \
  ts:TIMESTAMP,user_id:STRING,outcome:STRING,tier:STRING,model:STRING,prompt_chars:INTEGER,input_tokens:INTEGER,output_tokens:INTEGER,tokens_used:INTEGER,daily_limit:INTEGER,pii_engine:STRING,pii_findings:STRING,pii_prompt_redacted:BOOLEAN,pii_response_findings:STRING \
  || true

cat <<EOF

Setup complete. Remaining manual steps:
  1. Vertex Model Garden → enable "Claude Opus 4.8" (premium tier).
     Gemini models are available by default.
  2. Export env for deploy.sh:
       export PROJECT_ID=$PROJECT_ID REGION=$REGION BQ_DATASET=$BQ_DATASET
       export MODEL_ARMOR_TEMPLATE=projects/$PROJECT_ID/locations/$REGION/templates/$MA_TEMPLATE
EOF
