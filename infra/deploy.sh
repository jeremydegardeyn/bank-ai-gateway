#!/usr/bin/env bash
# Deploy gateway + UI to Cloud Run (both scale to zero → ~$0 idle cost).
# Each service directory is self-contained: `--source <dir>` builds its Dockerfile.
# Usage: PROJECT_ID=my-project MODEL_ARMOR_TEMPLATE=projects/.../templates/bank-pii-guard ./deploy.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
BQ_DATASET="${BQ_DATASET:-ai_gateway}"
MODEL_ARMOR_TEMPLATE="${MODEL_ARMOR_TEMPLATE:-}"
# Google sign-in: OAuth web client ID (the UI URL must be in its authorized
# JavaScript origins) and the email→persona mapping. Both are deploy-time
# config — never commit real values.
#   PERSONA_EMAILS="manager:a@corp.com;analyst:b@corp.com;auditor:c@gmail.com"
GOOGLE_OAUTH_CLIENT_ID="${GOOGLE_OAUTH_CLIENT_ID:-}"
PERSONA_EMAILS="${PERSONA_EMAILS:-}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "── Deploying gateway (private — IAM-authenticated callers only)…"
gcloud run deploy ai-gateway \
  --project "$PROJECT_ID" --region "$REGION" \
  --source "$REPO_ROOT/gateway" \
  --min-instances 0 --memory 512Mi \
  --no-allow-unauthenticated \
  --set-env-vars "^@^GCP_PROJECT=$PROJECT_ID@GCP_REGION=$REGION@BQ_DATASET=$BQ_DATASET@MODEL_ARMOR_TEMPLATE=$MODEL_ARMOR_TEMPLATE@FIRESTORE_DATABASE=ai-gateway@PERSONA_EMAILS=$PERSONA_EMAILS"

GATEWAY_URL=$(gcloud run services describe ai-gateway --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')

echo "── Deploying UI (public; authenticates to the gateway with an ID token)…"
gcloud run deploy ai-gateway-ui \
  --project "$PROJECT_ID" --region "$REGION" \
  --source "$REPO_ROOT/ui" \
  --min-instances 0 --memory 512Mi \
  --allow-unauthenticated \
  --set-env-vars "GATEWAY_URL=$GATEWAY_URL,GOOGLE_OAUTH_CLIENT_ID=$GOOGLE_OAUTH_CLIENT_ID"

echo "── Granting the UI's service account permission to invoke the gateway…"
UI_SA=$(gcloud run services describe ai-gateway-ui --project "$PROJECT_ID" --region "$REGION" --format='value(spec.template.spec.serviceAccountName)')
gcloud run services add-iam-policy-binding ai-gateway \
  --project "$PROJECT_ID" --region "$REGION" \
  --member "serviceAccount:$UI_SA" --role roles/run.invoker

UI_URL=$(gcloud run services describe ai-gateway-ui --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')
echo ""
echo "Done."
echo "  UI:      $UI_URL"
echo "  Gateway: $GATEWAY_URL (private)"
