# Placement Engine (`placement-engine`)

> **Autonomous AI-driven campus recruitment scribe and schedule tracker for NITK Surathkal.**

The **Placement Engine** is a single-tenant backend service that monitors official Telegram placement channels, auto-joins company-specific subchannels, extracts structured timeline events via Groq LPU inference (`openai/gpt-oss-20b`), and manages a pre-interview recruitment state machine.

> [!WARNING]
> **Status: pre-production.** A verified review (2026-10-21) found silent-failure gaps in ingestion, extraction and storage, and no API authentication. See `PROJECT_STATE.md` §0.2 for the list and §12 for the remediation plan in progress.

---

## Architecture Overview

```
                      ┌────────────────────────────────────────┐
                      │ Telegram Ingestion (Telethon MTProto)  │
                      │  • Main Feed & Test Feed Listener       │
                      │  • Auto-Join Subchannel Invite Links   │
                      │  • 24-Hour Bounded Backfill            │
                      │  • Sliding Debounce Buffer (90s)       │
                      └──────────────────┬─────────────────────┘
                                         │ Consolidated Payload
                                         ▼
                      ┌────────────────────────────────────────┐
                      │    AI Brain & State-Machine Parser     │
                      │  • Groq LPU: openai/gpt-oss-20b        │
                      │  • IST (+05:30) Temporal Anchoring     │
                      │  • Schedule Context Injection          │
                      │  • JSON-object mode + Pydantic checks  │
                      └──────────────────┬─────────────────────┘
                                         │ Structured Actions
                                         ▼
                      ┌────────────────────────────────────────┐
                      │ State Machine & CRUD Execution Engine  │
                      │  • CREATE_EVENT / EXTEND_DEADLINE      │
                      │  • RESCHEDULE_EVENT / REPLACE_FORM     │
                      │  • Criteria Relaxation Re-Triage       │
                      │  • PostgreSQL / SQLite (AsyncSession)  │
                      └──────────────────┬─────────────────────┘
                                         │ REST API
                                         ▼
                      ┌────────────────────────────────────────┐
                      │ React Native Mobile Command Center     │
                      │ (Expo SDK 57 / Android)                │
                      └────────────────────────────────────────┘
```

---

## Key Capabilities

1. **Two-Tier Telegram Ingestion**:
   - **Tier 1 (Root Feeds)**: Monitors designated broadcast feeds (`MAIN` and `TEST`) for company announcement threads and private/public invite links (`t.me/+...` or `t.me/...`).
   - **Tier 2 (Company Subchannels)**: Automatically requests to join detected subchannels, initializes them into `SILENT_TRACK` mode, runs a 24-hour backfill bootstrap, and registers them in the dynamic monitoring pool.
   - **Debouncing & Pre-Filtering**: Filters coordinator chatter/emojis and buffers multi-part messages over a sliding window (default: 90 seconds) to feed coherent blocks to the LLM.

2. **AI Structured Extraction Engine**:
   - Uses **`openai/gpt-oss-20b`** hosted on Groq for ultra-low latency inference.
   - Prompt injected with real-time **Indian Standard Time (IST, UTC+05:30)** anchoring and known company schedules.
   - Automatic fallback to regex-based IST temporal extraction on unstructured bursts. ⚠ *Under revision: verified to create wrong events and to fire on valid empty LLM results; removal planned in Phase 0.5.*

3. **Pre-Interview State Machine**:
   - **Event Lifecycle**: Point-in-time deadlines (`IRIS_DEADLINE`, `FORM_SUBMISSION`) and duration events (`OA_WINDOW`, `PPT`, `INTERVIEW`, `CUSTOM`).
   - **Form Versioning (`REPLACE_FORM`)**: When coordinators post updated Google Forms, the engine inserts a new version (`version` + 1) with the new link and deadline and `user_status = PENDING`. ⚠ *The previous version currently stays visible; hiding superseded forms is planned.*
   - **Criteria Relaxation Re-Triage**: Applies only to `SILENT_TRACK` roles and only proposes re-triage — it never changes the tracking mode. `IGNORED` is a terminal, human-only status. See the binding invariants in `PROJECT_STATE.md` §0.1. ⚠ *Today every status note re-triages SILENT_TRACK roles; the relaxation-only rule lands in Phase 0.5.*
   - **Feed Isolation**: Companies carry `environment_type = 'MAIN' | 'TEST'` and the timeline, companies and triage queries filter by it. ⚠ *Name-based company lookups during extraction do not filter by environment yet (gap H5).*

---

## Project Structure

```
placement-engine/
├── app/
│   ├── ai/
│   │   ├── client.py           # Async OpenAI client configured for Groq LPU
│   │   ├── dispatcher.py       # State-machine dispatcher and mutation router
│   │   ├── extractor.py        # LLM extraction call with fallback JSON sanitization
│   │   ├── prompts.py          # Dynamic system prompt builder with IST anchoring
│   │   ├── rule_extractor.py   # Regex-based deterministic IST relative date parser
│   │   └── schemas.py          # Pydantic v2 schemas for extraction actions
│   ├── routers/
│   │   ├── companies.py        # Company and role-track CRUD endpoints
│   │   ├── events.py           # Timeline event mutations and lifecycle routes
│   │   ├── settings.py         # Student profile and system config API
│   │   └── triage.py           # Pending triage hub accept/ignore endpoints
│   ├── telegram/
│   │   ├── backfill.py         # 24-hour message backfill for newly joined channels
│   │   ├── client.py           # Telethon client factory
│   │   ├── debouncer.py        # In-memory sliding window debouncer
│   │   ├── listener.py         # Central two-tier Telegram event router
│   │   └── pre_filter.py       # Rule-based chatter and noise gate
│   ├── config.py               # Pydantic BaseSettings application configuration
│   ├── crud.py                 # Async database queries and state-machine transitions
│   ├── database.py             # SQLAlchemy 2.0 async engine and session factory
│   ├── main.py                 # FastAPI application lifespan & payload bridge
│   └── models.py               # Declarative SQLAlchemy ORM models
├── scripts/
│   ├── clean_test_data.py      # Cleans test/sandbox records from MAIN environment
│   ├── login_telegram.py       # CLI interactive MTProto session authenticator
│   └── test_live_ingestion.py  # Live sandbox integration harness
├── requirements.txt            # Production Python dependencies
├── .env.example                # Environment variable reference
└── verify_*.py                 # Phase verification and test suites
```

---

## Getting Started

### 1. Prerequisites

- **Python 3.12+** — production targets 3.12; the local dev venv currently runs 3.14, so keep code 3.12-compatible
- **PostgreSQL** (Dockerized or managed) OR **SQLite** (local development)
- **Telegram API Credentials** (`api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org/apps))
- **Groq API Key** (from [console.groq.com](https://console.groq.com))

### 2. Installation

```bash
# Navigate to the engine directory
cd placement-engine

# Create virtual environment
python -m venv venv

# Activate virtual environment
# Windows (PowerShell):
.\venv\Scripts\Activate.ps1
# Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Environment Configuration

Copy the template and configure your secrets:

```bash
cp .env.example .env
```

| Variable | Description | Default |
| :--- | :--- | :--- |
| `DATABASE_URL` | SQLAlchemy async connection string | `postgresql+asyncpg://...` or `sqlite+aiosqlite:///./dev.db` |
| `APP_ENV` | Runtime environment (`development` / `production`) | `development` |
| `TELEGRAM_API_ID` | Telegram API ID from my.telegram.org | `0` |
| `TELEGRAM_API_HASH` | Telegram API Hash from my.telegram.org | `""` |
| `TELEGRAM_SESSION_NAME`| Name of local session file (used when no session string is set) | `placement_session` |
| `TELEGRAM_SESSION_STRING`| Optional Telethon `StringSession`; takes precedence over the file | `""` |
| `TELEGRAM_PHONE` | Your Telegram phone number for initial authentication | `""` |
| `MAIN_FEED_CHANNEL_ID` | Production Telegram broadcast channel ID | `None` |
| `TEST_FEED_CHANNEL_ID` | Sandbox Telegram test channel ID | `None` |
| `DEBOUNCE_DELAY_SECONDS`| Buffer window for message bursts | `90.0` |
| `GROQ_API_KEY` / `LLM_API_KEY` | API key for the extraction LLM (`LLM_API_KEY` wins if both are set) | `""` |
| `LLM_BASE_URL` | OpenAI-compatible endpoint | `https://api.groq.com/openai/v1` |
| `LLM_MODEL` | LLM model identifier | `openai/gpt-oss-20b` |

### 4. Authenticate Telegram Client

Before starting the server, create your authenticated MTProto session file:

```bash
python scripts/login_telegram.py
```
Follow the interactive prompt to enter the SMS or in-app login code and 2FA password. This creates `placement_session.session`.

### 5. Start Backend Server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

> [!WARNING]
> With `TELEGRAM_API_ID` set, the server connects your **live** Telegram session, processes the configured feeds and auto-joins invite links. For API-only work, set `TELEGRAM_API_ID=0` for that process only — don't edit `.env`.
Interactive OpenAPI documentation will be accessible at:
- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`

---

## Verification & Testing

The repository includes verification suites that run against in-memory SQLite with the LLM and Telegram mocked. ⚠ `verify_phase3.py` does not yet bind an in-memory engine (it can read the configured database; fix in Phase 0.2), and no suite contains realistic multi-sentence announcements yet.

```bash
# Run all verification suites:
python verify_phase1.py   # Architecture, models, and session safety
python verify_phase2.py   # CRUD engine, state cascades, and REST API
python verify_phase3.py   # Two-tier Telegram ingestion & debouncer
python verify_phase4.py   # LLM structured extraction & state-machine actions
python verify_triage.py   # Triage hub, criteria relaxation, and role tracking
python verify_settings.py # Student profile settings API
```

---

## Database Maintenance

> [!CAUTION]
> **Do not run `scripts/clean_test_data.py` against real data.** It *hard-deletes* every `MAIN` company whose name merely contains `test`, `dummy`, `fallback`, `mock` or `acme` (e.g. "Latest updates…", Testbook, Acme Solar). A TEST-only, dry-run-by-default version is planned for Phase 0.6.

---

## Production Deployment

### Docker Deployment

> [!CAUTION]
> **Superseded — do not build this.** There is no `.dockerignore`, so `COPY . .` would bake `.env`, `placement_session.session`, `dev.db` and the local venv into image layers. It also uses Python 3.11 while production targets 3.12, and it runs Telethon inside the API process. The hardened multi-stage image (API and worker containers, secrets on mounted volumes) is part of Phase 1.

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Cloudflare Tunnel (HTTPS Ingress)
```bash
cloudflared tunnel --url http://localhost:8000
```
Use the assigned Cloudflare HTTPS URL as the backend URL in the mobile **Command Center**. Quick-tunnel URLs change on every restart, and the API has no authentication yet. Production will use a named tunnel on an owned domain plus a shared API key (Phases 0.6 and 1).
