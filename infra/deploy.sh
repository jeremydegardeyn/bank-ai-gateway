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
# Remote MCP servers this gateway may call, as "name=url;name=url". Each is a private
# Cloud Run service that must separately grant run.invoker to GATEWAY_SA.
MCP_SERVERS="${MCP_SERVERS:-}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# The gateway gets its own identity rather than the project's default compute SA.
# That was tolerable while it only called Vertex. It stopped being tolerable the
# moment another team granted an identity access to their banking tools, because the
# PUBLIC UI service runs as the default SA too — granting "the gateway" would have
# granted the internet-facing service in the same breath.
GATEWAY_SA="${GATEWAY_SA:-ai-gateway-sa@${PROJECT_ID}.iam.gserviceaccount.com}"
if ! gcloud iam service-accounts describe "$GATEWAY_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "── Creating the gateway service account…"
  gcloud iam service-accounts create "${GATEWAY_SA%%@*}" --project "$PROJECT_ID" \
    --display-name "Bank AI Gateway (Cloud Run runtime)"
  for role in roles/aiplatform.user roles/datastore.user roles/bigquery.dataEditor \
              roles/bigquery.jobUser roles/modelarmor.user; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
      --member "serviceAccount:$GATEWAY_SA" --role "$role" --condition=None >/dev/null
  done
fi

# The UI is PUBLIC and holds no project roles at all: it verifies a Google sign-in
# token and forwards the email to the gateway, and every capability it needs is
# granted per-target (run.invoker on the gateway, below). It ran as the project's
# default compute service account until 2026-09-07 — which carries roles/owner. An
# internet-facing container with project owner is the single worst identity in a
# demo, and it is invisible because nothing in the app ever uses the extra reach.
UI_SA="${UI_SA:-ai-gateway-ui-sa@${PROJECT_ID}.iam.gserviceaccount.com}"
if ! gcloud iam service-accounts describe "$UI_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "── Creating the UI service account…"
  gcloud iam service-accounts create "${UI_SA%%@*}" --project "$PROJECT_ID" \
    --display-name "Bank AI Gateway UI (Cloud Run runtime, no project roles)"
fi

echo "── Deploying gateway (private — IAM-authenticated callers only)…"
gcloud run deploy ai-gateway \
  --project "$PROJECT_ID" --region "$REGION" \
  --source "$REPO_ROOT/gateway" \
  --min-instances 0 --memory 512Mi \
  --service-account "$GATEWAY_SA" \
  --no-allow-unauthenticated \
  --set-env-vars "^|^GCP_PROJECT=$PROJECT_ID|GCP_REGION=$REGION|BQ_DATASET=$BQ_DATASET|MODEL_ARMOR_TEMPLATE=$MODEL_ARMOR_TEMPLATE|FIRESTORE_DATABASE=ai-gateway|PERSONA_EMAILS=$PERSONA_EMAILS|MCP_SERVERS=$MCP_SERVERS"

GATEWAY_URL=$(gcloud run services describe ai-gateway --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')

echo "── Deploying UI (public; authenticates to the gateway with an ID token)…"
gcloud run deploy ai-gateway-ui \
  --project "$PROJECT_ID" --region "$REGION" \
  --source "$REPO_ROOT/ui" \
  --min-instances 0 --memory 512Mi \
  --service-account "$UI_SA" \
  --allow-unauthenticated \
  --set-env-vars "GATEWAY_URL=$GATEWAY_URL,GOOGLE_OAUTH_CLIENT_ID=$GOOGLE_OAUTH_CLIENT_ID"

echo "── Granting the UI's service account permission to invoke the gateway…"
gcloud run services add-iam-policy-binding ai-gateway \
  --project "$PROJECT_ID" --region "$REGION" \
  --member "serviceAccount:$UI_SA" --role roles/run.invoker

UI_URL=$(gcloud run services describe ai-gateway-ui --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')
echo ""
echo "Done."
echo "  UI:      $UI_URL (public, as $UI_SA)"
echo "  Gateway: $GATEWAY_URL (private, as $GATEWAY_SA)"
echo "  MCP:     ${MCP_SERVERS:-<none registered>}"
