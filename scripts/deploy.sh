#!/usr/bin/env bash
# ============================================================
# BLK PHX LABS Cloud Function Deploy Script
# Usage: ./scripts/deploy.sh
# Run from repo root.
# ============================================================

set -euo pipefail

# ---- CONFIG — edit these or set as env vars ----
PROJECT_ID="${GCP_PROJECT_ID:-your-gcp-project-id}"
REGION="${GCP_REGION:-us-central1}"
BUCKET_NAME="labmind-briefing-ops"
FUNCTION_NAME="blkphxlabs-audio-engine"
ENTRY_POINT="generate_content"
RUNTIME="python311"
GEMINI_SECRET="gemini-api-key"
# ------------------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[DEPLOY]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $1"; }
fail() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ---- Preflight ----
command -v gcloud >/dev/null 2>&1 || fail "gcloud CLI not found"
[[ "$PROJECT_ID" == "your-gcp-project-id" ]] && fail "Set GCP_PROJECT_ID env var"
[[ -f "cloud-function/main.py" ]] || fail "Run from repo root — cloud-function/main.py not found"
[[ -f "cloud-function/requirements.txt" ]] || fail "cloud-function/requirements.txt not found"

gcloud config set project "$PROJECT_ID"

log "Deploying $FUNCTION_NAME to $REGION..."

gcloud functions deploy "$FUNCTION_NAME" \
  --gen2 \
  --runtime="$RUNTIME" \
  --region="$REGION" \
  --source="./cloud-function" \
  --entry-point="$ENTRY_POINT" \
  --memory="2Gi" \
  --timeout="540s" \
  --trigger-event-filters="type=google.cloud.storage.object.v1.finalized" \
  --trigger-event-filters="bucket=${BUCKET_NAME}" \
  --trigger-location="us" \
  --set-env-vars="GCP_PROJECT=${PROJECT_ID}" \
  --set-secrets="GEMINI_API_KEY=${GEMINI_SECRET}:latest,WEBHOOK_SECRET=briefing-webhook-secret:latest" \
  --quiet

log "Deployment complete."

# The Gemini key lives only in Secret Manager, never in env vars (deploy output prints those).
# To rotate it, add a new version; the next cold start picks it up:
#   printf '%s' "$NEW_KEY" | gcloud secrets versions add ${GEMINI_SECRET} --data-file=-

# ---- Print status ----
echo ""
log "Deployment Summary:"
gcloud functions describe "$FUNCTION_NAME" --region="$REGION" \
  --format="table(name, state, updateTime, serviceConfig.uri)"

echo ""
log "Tail logs with:"
echo "  gcloud functions logs read $FUNCTION_NAME --region=$REGION --limit=50"
echo ""
log "Briefing:"
echo "  https://blkphxlabs.com/briefing/"
