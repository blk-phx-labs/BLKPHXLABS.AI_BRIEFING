#!/usr/bin/env bash
# ============================================================
# BLK PHX LABS GCP Setup Script
# Run once to provision all required GCP resources.
# Usage: ./scripts/setup.sh
# ============================================================

set -euo pipefail

# ---- CONFIG — edit these before running ----
PROJECT_ID="${GCP_PROJECT_ID:-your-gcp-project-id}"
REGION="${GCP_REGION:-us-central1}"
BUCKET_NAME="labmind-briefing-ops"
SERVICE_ACCOUNT_NAME="blkphxlabs-function-sa"
# --------------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[SETUP]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $1"; }
fail() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ---- Preflight checks ----
command -v gcloud >/dev/null 2>&1 || fail "gcloud CLI not found. Install from https://cloud.google.com/sdk"
[[ "$PROJECT_ID" == "your-gcp-project-id" ]] && fail "Set GCP_PROJECT_ID env var or edit this script"

log "Setting project: $PROJECT_ID"
gcloud config set project "$PROJECT_ID"

# ---- Enable required APIs ----
log "Enabling required APIs (this may take a minute)..."
gcloud services enable \
  cloudfunctions.googleapis.com \
  cloudbuild.googleapis.com \
  storage.googleapis.com \
  texttospeech.googleapis.com \
  drive.googleapis.com \
  gmail.googleapis.com \
  run.googleapis.com \
  eventarc.googleapis.com \
  --quiet

log "APIs enabled."

# ---- Create GCS bucket ----
if gsutil ls "gs://${BUCKET_NAME}" &>/dev/null; then
  warn "Bucket gs://${BUCKET_NAME} already exists — skipping."
else
  log "Creating GCS bucket: ${BUCKET_NAME}"
  gsutil mb -p "$PROJECT_ID" -l "$REGION" -b on "gs://${BUCKET_NAME}"

  # Private: holds the job queue (Gmail alert payloads), memory and assets.
  log "Bucket created (private)."
fi

# ---- Create public briefing bucket ----
# Only the published video, audio and briefing JSON go here. allUsers gets
# legacyObjectReader (read by exact name, no listing), and only blkphxlabs.com
# may fetch from a browser script.
PUBLIC_BUCKET="blkphxlabs-briefing"
if gsutil ls "gs://${PUBLIC_BUCKET}" &>/dev/null; then
  warn "Bucket gs://${PUBLIC_BUCKET} already exists — skipping."
else
  log "Creating public briefing bucket: ${PUBLIC_BUCKET}"
  gcloud storage buckets create "gs://${PUBLIC_BUCKET}" --project="$PROJECT_ID" --location=US --uniform-bucket-level-access
  echo '[{"origin": ["https://blkphxlabs.com"], "method": ["GET", "HEAD"], "responseHeader": ["Content-Type", "Range", "Content-Range", "Accept-Ranges"], "maxAgeSeconds": 3600}]' > /tmp/briefing-cors.json
  gcloud storage buckets update "gs://${PUBLIC_BUCKET}" --cors-file=/tmp/briefing-cors.json
  gcloud storage buckets add-iam-policy-binding "gs://${PUBLIC_BUCKET}" \
    --member=allUsers --role=roles/storage.legacyObjectReader --format=none
  log "Public briefing bucket created."
fi

# ---- Create required GCS folders (via placeholder objects) ----
log "Initializing bucket structure..."
for prefix in queue/ memory/ assets/; do
  echo "" | gsutil cp - "gs://${BUCKET_NAME}/${prefix}.keep" 2>/dev/null || true
done

# ---- Create service account ----
SA_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

if gcloud iam service-accounts describe "$SA_EMAIL" &>/dev/null; then
  warn "Service account $SA_EMAIL already exists — skipping."
else
  log "Creating service account: $SA_EMAIL"
  gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" \
    --display-name="BLK PHX LABS Function SA" \
    --description="Service account for BLK PHX LABS Cloud Function"
fi

# ---- Grant service account roles ----
log "Granting IAM roles to $SA_EMAIL..."

ROLES=(
  "roles/storage.admin"
  "roles/texttospeech.user"
  "roles/drive.file"
  "roles/cloudfunctions.invoker"
)

for role in "${ROLES[@]}"; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="$role" \
    --quiet 2>/dev/null || warn "Could not bind $role (may already exist)"
done

log "IAM roles granted."

# ---- Verify GEMINI_API_KEY is set ----
if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  warn "GEMINI_API_KEY not set as environment variable."
  warn "Set it with:"
  warn "  export GEMINI_API_KEY='AIzaSy...'"
  warn "  gcloud run services update blkphxlabs-audio-engine --update-env-vars GEMINI_API_KEY=\$GEMINI_API_KEY"
  warn "You'll need to do this after deploying the Cloud Function."
else
  log "GEMINI_API_KEY is set — will configure Cloud Run after deploy."
fi

# ---- Summary ----
echo ""
echo -e "${GREEN}==============================${NC}"
echo -e "${GREEN}  BLK PHX LABS GCP Setup Complete  ${NC}"
echo -e "${GREEN}==============================${NC}"
echo ""
echo "  Project:         $PROJECT_ID"
echo "  Region:          $REGION"
echo "  Bucket:          gs://$BUCKET_NAME"
echo "  Service Account: $SA_EMAIL"
echo ""
echo "Next steps:"
echo "  1. Upload a background video:  gsutil cp your_background.mp4 gs://${BUCKET_NAME}/assets/background_loop.mp4"
echo "  2. Deploy Cloud Function:      ./scripts/deploy.sh"
echo "  3. Deploy Apps Script:         see README.md"
echo ""
