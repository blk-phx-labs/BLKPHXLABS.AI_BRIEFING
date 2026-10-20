# BLK PHX LABS Intelligence Engine

An autonomous biotech intelligence pipeline that scrapes Google Alerts, ranks and analyzes articles with a three-pass Gemini RAG engine, and publishes a spoken audio/video briefing to [blkphxlabs.com/briefing](https://blkphxlabs.com/briefing/) — daily at 6 AM.

```
Gmail (Google Alerts)
  └─► Apps Script (Scout)
        └─► GCS trigger_job.json
              └─► Cloud Function (RAG Engine)
                    ├─► Scrape articles (ThreadPoolExecutor)
                    ├─► Semantic relevance scoring (Gemini)
                    ├─► Three-pass analysis (Extract → Analyze → Narrate)
                    ├─► TTS synthesis (Google Cloud TTS)
                    ├─► Video production (MoviePy)
                    └─► Publish (video, audio, briefing JSON → public bucket; Drive archive)
```

---

## Repository Structure

```
blkphxlabs/
├── apps-script/
│   └── blkphxlabs.js            # Gmail crawler + GCS dispatcher (Google Apps Script)
├── cloud-function/
│   ├── main.py               # RAG engine (Cloud Function entry point)
│   └── requirements.txt
├── scripts/
│   ├── setup.sh              # One-command GCP project setup
│   └── deploy.sh             # Deploy cloud function
├── docs/
│   ├── DEPLOYMENT_GUIDE.md   # Step-by-step setup and troubleshooting
│   └── DIAGNOSIS.md          # V33→V34 bug analysis
├── .gitignore
└── README.md
```

---

## Prerequisites

- Google Cloud project with billing enabled
- Google Cloud CLI (`gcloud`) authenticated
- Google Apps Script project (linked to your Google account)
- Google Alerts configured and emailing your Gmail

---

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/fabzy4L/BLKPHXLABS.AI_BRIEFING.git
cd blkphxlabs-ai
cp .env.example .env
# Edit .env with your values
```

### 2. Run GCP setup

```bash
chmod +x scripts/setup.sh
./scripts/setup.sh
```

This will:
- Enable required APIs
- Create the GCS bucket
- Set environment variables on your Cloud Run service
- Create the Drive folder

### 3. Deploy Cloud Function

```bash
chmod +x scripts/deploy.sh
./scripts/deploy.sh
```

### 4. Deploy Apps Script

```bash
# Install clasp (Google Apps Script CLI)
npm install -g @google/clasp
clasp login

cd apps-script
clasp create --type standalone --title "BLK PHX LABS Scout"
clasp push

# Set script properties (secrets) in Apps Script UI:
# Project Settings → Script Properties → Add
#   GEMINI_API_KEY    = your key
#   RAW_PRIVATE_KEY   = your service account private key
#   CLIENT_EMAIL      = your service account email

```

### 5. Verify

```bash
# Check the published briefing
open https://blkphxlabs.com/briefing/

# Tail Cloud Function logs
gcloud functions logs read blkphxlabs-audio-engine --limit=50
```

---

## Configuration

### Apps Script (`apps-script/blkphxlabs.js`)

| Constant | Description |
|---|---|
| `TARGET_FOLDER_ID` | Google Drive folder for briefing docs |
| `SEARCH_QUERY` | Gmail search filter for Google Alerts |
| `BUCKET_NAME` | GCS bucket name |
| `BRIEFING_SHEET_ID` | Google Sheets ID for article log |

### Cloud Function (`cloud-function/main.py`)

| Constant | Description |
|---|---|
| `VOICE_NAME` | Google TTS voice (default: `en-US-Journey-D`) |
| `GEMINI_URL` | Gemini API endpoint |
| `DRIVE_FOLDER_ID` | Drive folder for video archives |
| `RAG_CONTEXT_CHAR_LIMIT` | Max chars fed to Gemini (default: 20,000) |

---

## Secrets Management

**No secrets are ever stored in this repository.**

| Secret | Where It Lives |
|---|---|
| `GEMINI_API_KEY` | Cloud Run environment variable |
| `RAW_PRIVATE_KEY` | Apps Script Script Properties |
| `CLIENT_EMAIL` | Apps Script Script Properties |

### Set Cloud Run env var

```bash
gcloud run services update blkphxlabs-audio-engine \
  --update-env-vars GEMINI_API_KEY="AIzaSy..."
```

### Set Apps Script properties

Apps Script editor → Project Settings → Script Properties → Add property

---

## Pipeline Versions

| Version | Key Changes |
|---|---|
| V31 | Initial release — keyword taxonomy scoring |
| V32 | Apps Script diagnostic logging, always-dispatch patch |
| V33 | Removed hard-drop of zero-relevance articles |
| V34 | **Semantic relevance scoring**, 20k context budget, emergency fallback |

---

## Troubleshooting

See [`docs/DEPLOYMENT_GUIDE.md`](docs/DEPLOYMENT_GUIDE.md) for a full troubleshooting table.

**Most common issue:** Dashboard shows `SOURCES: 0`

**Quick diagnosis:**
```
1. Gmail search: from:googlealerts-noreply@google.com
   → No results? Google Alerts not arriving.

2. Run compileWeeklyBriefing manually → View logs
   → [DIAGNOSTIC] Threads found: 0? Gmail search failing.
   → [DIAGNOSTIC] Fresh articles: 0? All articles in history.

3. Cloud Function logs:
   gcloud functions logs read blkphxlabs-audio-engine --limit=50
   → Job loaded: 0 articles? Apps Script not dispatching.
```

---

## License

MIT
