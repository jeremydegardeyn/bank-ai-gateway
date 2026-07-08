#!/usr/bin/env bash
# Deploy gateway + UI to Cloud Run (both scale to zero → ~$0 idle cost).
# Usage: PROJECT_ID=my-project MODEL_ARMOR_TEMPLATE=projects/.../templates/bank-pii-guard ./deploy.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
BQ_DATASET="${BQ_DATASET:-ai_gateway}"
MODEL_ARMOR_TEMPLATE="${MODEL_ARMOR_TEMPLATE:-}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "── Deploying gateway…"
gcloud run deploy ai-gateway \
  --project "$PROJECT_ID" --region "$REGION" \
  --source "$REPO_ROOT" \
  --dockerfile gateway/Dockerfile \
  --min-instances 0 --memory 512Mi \
  --no-allow-unauthenticated \
  --set-env-vars "GCP_PROJECT=$PROJECT_ID,GCP_REGION=$REGION,BQ_DATASET=$BQ_DATASET,MODEL_ARMOR_TEMPLATE=$MODEL_ARMOR_TEMPLATE"

GATEWAY_URL=$(gcloud run services describe ai-gateway --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')

echo "── Deploying UI…"
gcloud run deploy ai-gateway-ui \
  --project "$PROJECT_ID" --region "$REGION" \
  --source "$REPO_ROOT" \
  --dockerfile ui/Dockerfile \
  --min-instances 0 --memory 512Mi \
  --allow-unauthenticated \
  --set-env-vars "GATEWAY_URL=$GATEWAY_URL"

# Let the UI's service account call the private gateway.
UI_SA=$(gcloud run services describe ai-gateway-ui --project "$PROJECT_ID" --region "$REGION" --format='value(spec.template.spec.serviceAccountName)')
gcloud run services add-iam-policy-binding ai-gateway \
  --project "$PROJECT_ID" --region "$REGION" \
  --member "serviceAccount:$UI_SA" --role roles/run.invoker

echo "Done. UI: $(gcloud run services describe ai-gateway-ui --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
echo "Note: for the private-gateway setup the UI must send an ID token; for a"
echo "quick demo you can instead deploy the gateway with --allow-unauthenticated."
