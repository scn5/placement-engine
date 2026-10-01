# PROJECT_STATE.md — Placement Engine Backend
**Last Updated:** 2026-10-02 | **Version:** 0.4.0 | **Branch:** `main`

> Single source of truth for the backend's operational status, architecture, and immediate backlog. Cross-referenced against `MASTER_SPEC.md` and `PRODUCT REQUIREMENTS DOCUMENT.md`.

---

## 1. Repository Identity

| Field | Value |
|---|---|
| **Project Name** | Placement Command Center — Backend Engine |
| **Repo** | `scn5/placement-engine` |
| **Runtime** | Python 3.14, FastAPI 0.111+, Uvicorn |
| **Local Bind** | `0.0.0.0:8080` |
| **Public Exposure** | Cloudflare Quick Tunnel (`cloudflared`) → HTTPS endpoint consumed by `command-center` Android app |
| **DB (dev)** | SQLite via `aiosqlite` (`./dev.db`) |
| **DB (target)** | PostgreSQL (Dockerized, OCI Always Free VM) |
| **LLM Endpoint** | `openai/gpt-oss-20b` via OpenAI-compatible base URL (Groq LPU) |
| **Tenancy** | Strictly single-tenant — no auth layer, no multi-user scope |

---

## 2. Source File Inventory

```
placement-engine/
├── .env                          # Local dev secrets (git-ignored)
├── .env.example                  # Safe credential template (committed)
├── .gitignore                    # Excludes venv/, *.env, __pycache__, *.log
├── requirements.txt              # Pinned dependencies
├── MASTER_SPEC.md                # System architecture & DB schema source of truth
├── PRODUCT REQUIREMENTS DOCUMENT.md
├── verify_phase1.py              # Schema & model smoke tests (no DB required)
├── verify_phase2.py              # CRUD & router tests (in-memory SQLite)
├── verify_phase3.py              # Telegram ingestion mock suite
├── verify_phase4.py              # LLM extraction & dispatch mock suite
├── dev.db                        # Local SQLite database (git-ignored)
│
└── app/
    ├── __init__.py
    ├── config.py                 # Pydantic Settings — all env vars
    ├── database.py               # Async engine, session factory, get_db()
    ├── models.py                 # SQLAlchemy 2.0 ORM models
    ├── schemas.py                # Pydantic v2 API request/response schemas
    ├── crud.py                   # Async CRUD operations (select/update/returning)
    ├── main.py                   # FastAPI app factory, lifespan, CORS, routers
    │
    ├── routers/
    │   ├── __init__.py
    │   ├── companies.py          # /companies endpoints
    │   └── events.py             # /events endpoints
    │
    ├── telegram/
    │   ├── __init__.py
    │   ├── client.py             # Telethon StringSession factory
    │   ├── pre_filter.py         # Regex noise gate (should_filter_noise)
    │   ├── debouncer.py          # Sliding-window ChannelDebouncer
    │   ├── backfill.py           # 24h bounded historical fetcher
    │   └── listener.py           # TelegramIngestionService (event handlers)
    │
    └── ai/
        ├── __init__.py
        ├── client.py             # AsyncOpenAI factory (base_url configurable)
        ├── schemas.py            # LLM extraction Pydantic models
        ├── prompts.py            # System prompt builder (UTC + context injection)
        ├── extractor.py          # LLM inference + ValidationError fallback
        └── dispatcher.py        # State-machine CRUD executor
```

---

## 3. Tech Stack & Dependencies

| Package | Version | Role |
|---|---|---|
| `fastapi` | ≥ 0.111.0 | ASGI web framework |
| `uvicorn[standard]` | ≥ 0.30.0 | ASGI server + hot-reload |
| `sqlalchemy` | ≥ 2.0.30 | ORM, async engine (`select`/`update`/`returning`) |
| `asyncpg` | ≥ 0.29.0 | PostgreSQL async driver (prod) |
| `aiosqlite` | ≥ 0.20.0 | SQLite async driver (dev + tests) |
| `pydantic` | ≥ 2.7.0 | Schema validation, `model_validate_json` |
| `pydantic-settings` | ≥ 2.2.0 | `BaseSettings` with `.env` loading |
| `python-dotenv` | ≥ 1.0.1 | `.env` file support |
| `telethon` | ≥ 1.36.0 | Telegram MTProto client |
| `openai` | ≥ 1.30.0 | AsyncOpenAI client (OpenAI-compatible endpoint) |

---

## 4. Configuration (`app/config.py`)

All settings are loaded from `.env` via `pydantic-settings`. Defaults allow the app to boot without any env vars set.

| Setting | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` | Override to `sqlite+aiosqlite:///./dev.db` in dev |
| `APP_ENV` | `development` | Environment label |
| `TELEGRAM_API_ID` | `0` | MTProto App ID (0 = listener disabled) |
| `TELEGRAM_API_HASH` | `""` | MTProto App Hash |
| `TELEGRAM_SESSION_STRING` | `""` | Telethon `StringSession` token (no disk file) |
| `TELEGRAM_PHONE` | `None` | Used for interactive first-auth only |
| `MAIN_FEED_CHANNEL_ID` | `None` | Production placement channel ID |
| `DEBOUNCE_DELAY_SECONDS` | `90.0` | Sliding window before batch dispatch |
| `LLM_API_KEY` | `""` | Bearer token for LLM endpoint |
| `LLM_BASE_URL` | `None` | Override for Groq / OpenRouter / local vLLM |
| `LLM_MODEL` | `openai/gpt-oss-20b` | Model identifier |

> **Dev `.env`** uses `DATABASE_URL=sqlite+aiosqlite:///./dev.db` and `TELEGRAM_API_ID=0`, so the server starts fully without Telegram credentials.

---

## 5. ORM Models (`app/models.py`)

### 5.1 `Company`
| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | Auto-increment |
| `name` | String(255) | Display name |
| `normalized_name` | String(255) UNIQUE | Lowercase stripped; lookup key |
| `main_feed_msg_id` | BigInteger | Single-ID current impl (spec calls for array) |
| `subchannel_id` | BigInteger UNIQUE | Telegram channel ID for routing |
| `tracking_mode` | String(32) | `ELIGIBLE` \| `SILENT_TRACK` \| `IGNORED` |
| `is_active` | Boolean | Soft-delete flag |
| `is_custom` | Boolean | Manual entry vs Telegram-discovered |
| `created_at` | DateTime(tz) | UTC default |

**Relationships:** `role_tracks` (cascade delete), `timeline_events` (cascade delete)

> **⚠ Schema Gap vs MASTER_SPEC:** `main_feed_msg_id` is a single `BigInteger`. The spec defines `main_feed_msg_ids BIGINT[]` (array) for multi-message thread stitching. Needs migration when switching to PostgreSQL.
> **⚠ Schema Gap:** `environment_type` column (`MAIN`/`TEST`) is absent — required for FR1 env isolation.
> **⚠ Schema Gap:** `tracking_mode` lives on `Company`, but spec places it on `role_tracks` (per-role triage state). Current location serves the Phase 2 API but will need reconciliation.

### 5.2 `RoleTrack`
| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `company_id` | FK → companies | CASCADE DELETE |
| `role_name` | String(255) | e.g. `DAA`, `BTSA`, `Software Engineer` |
| `drive_type` | String(32) | `FTE` \| `INTERNSHIP` \| `COMBINED` |
| `ctc_stipend_info` | Text | Package string |
| `lifecycle_status` | String(64) | `APPLICATION_WINDOW` \| `INTERVIEWS` \| … |
| `is_targeted_by_user` | Boolean | HITL tracking flag |
| `latest_status_note` | Text | Non-temporal status (e.g. "Shortlists delayed") |
| `updated_at` | DateTime(tz) | Auto-updated on write |

> **⚠ Schema Gap:** `tracking_mode` per role (spec §5 `role_tracks.tracking_mode`) is absent here; currently on `Company`.

### 5.3 `TimelineEvent`
| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `company_id` | FK → companies | CASCADE DELETE |
| `role_track_id` | FK → role_tracks | SET NULL on delete |
| `event_type` | String(64) | `IRIS_DEADLINE` \| `FORM_SUBMISSION` \| `PPT` \| `OA_WINDOW` \| `INTERVIEW` \| `CUSTOM` |
| `title` | String(255) | Human-readable label |
| `action_link` | Text | Form URL, OA portal, etc. |
| `start_time` | DateTime(tz) | Optional event start |
| `end_time` | DateTime(tz) NOT NULL | Deadline — drives all state logic |
| `user_status` | String(32) | `PENDING` \| `COMPLETED` |
| `completed_at` | DateTime(tz) | Timestamp of user completion |
| `is_custom` | Boolean | Manual vs auto-ingested |
| `version` | Integer | Form replacement counter |
| `created_at` | DateTime(tz) | |

**Computed Property — `display_state`:**
```
user_status=PENDING  + end_time > now  → "PENDING"          (Blue badge)
user_status=PENDING  + end_time < now  → "MISSED"           (Red badge)
user_status=COMPLETED + end_time > now → "COMPLETED_ACTIVE" (Green badge)
user_status=COMPLETED + end_time < now → "COMPLETED_EXPIRED"(Grey badge)
```
> SQLite tz-naïve datetime coercion is handled in `display_state` via `.replace(tzinfo=timezone.utc)`.

> **⚠ Schema Gap vs MASTER_SPEC:** `event_type` currently uses Phase 2 naming (`OA_WINDOW`, `IRIS_DEADLINE`) while spec/LLM schema uses `event_category` with values `OA`, `IRIS`, `FORM`, `GD`, etc. Needs a unified enum before the extractor can write to DB.
> **⚠ Schema Gap:** `round_number` (INTEGER) is absent — required for state-aware OA/Interview round counting.
> **⚠ Schema Gap:** `has_attachments` (Boolean) is absent — required for paperclip indicator (PRD FR-2).

---

## 6. REST API Endpoints

### 6.1 Registered Routes

#### Meta
| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Preflight check; returns `{"status": "healthy"}` |

#### Companies — `/companies`
| Method | Path | Request Body | Response |
|---|---|---|---|
| `POST` | `/companies/` | `CompanyCreate` | `CompanyResponse` (201) |
| `GET` | `/companies/` | `?tracking_mode=` | `List[CompanyResponse]` |
| `PATCH` | `/companies/{id}/tracking` | `ToggleTrackingPayload` | `CompanyResponse` |
| `POST` | `/companies/{id}/tracks` | `RoleTrackCreate` | `RoleTrackResponse` (201) |

#### Timeline Events — `/events`
| Method | Path | Request Body / Params | Response |
|---|---|---|---|
| `GET` | `/events/timeline` | `?drive_type=FTE` | `List[TimelineEventResponse]` |
| `POST` | `/events/` | `TimelineEventCreate` | `TimelineEventResponse` (201) |
| `PATCH` | `/events/{id}/complete` | `?completed=true` | `TimelineEventResponse` |
| `PATCH` | `/events/{id}/extend` | `ExtendDeadlinePayload` | `TimelineEventResponse` |
| `POST` | `/events/companies/{id}/replace-form` | `ReplaceFormPayload` | `TimelineEventResponse` (201) |

### 6.2 Missing Routes (Immediate Backlog)

> **🚨 Known 404 — Triage Hub:** The mobile `command-center` app calls `GET /triage` and `POST /triage/{id}/accept`. Neither route is registered. The app currently uses a client-side fallback (`[]`) but this must be resolved before triage functionality is live.

| Method | Path | Status | Priority |
|---|---|---|---|
| `GET` | `/triage` | ❌ Not implemented | **P0 — Mobile 404** |
| `POST` | `/triage/{id}/accept` | ❌ Not implemented | **P0 — Mobile 404** |
| `POST` | `/triage/{id}/ignore` | ❌ Not implemented | P1 |
| `GET` | `/companies/{id}` | ❌ Not implemented | P1 |
| `GET` | `/system-config` | ❌ Not implemented | P1 |
| `PUT` | `/system-config` | ❌ Not implemented | P1 |
| `POST` | `/notify/test` | ❌ Not implemented | P2 |

---

## 7. Telegram Ingestion Pipeline (`app/telegram/`)

### 7.1 Client (`client.py`)
- `create_telegram_client()` → `TelegramClient` using `StringSession`
- Session token lives in `TELEGRAM_SESSION_STRING` env var — **no `.session` files on disk**
- `flood_sleep_threshold=60` — auto-sleeps on `FloodWaitError` up to 60s

> **Activation gate:** Listener only starts if `settings.TELEGRAM_API_ID != 0`. The dev `.env` sets `TELEGRAM_API_ID=0`, so the server boots cleanly without credentials.

### 7.2 Pre-Filter (`pre_filter.py`)
`should_filter_noise(text, has_media) -> bool`

**Decision tree (in order):**
1. `has_media=True` → **KEEP** unconditionally (JD PDFs, schedule images)
2. `text` empty/whitespace → **DROP**
3. `len(text) < 8` → **DROP**
4. Matches `_ADMIN_CHATTER_RE` (SPOC routing, "dm me", "ping us") → **DROP**
5. No match in `_SIGNAL_RE` (dates, times, "deadline", "form", "oa", "interview", "iris", "ppt", "ctc", "shortlist", "http", …) → **DROP**
6. Otherwise → **KEEP**

### 7.3 Debouncer (`debouncer.py`)
`ChannelDebouncer` — one instance per process, keyed by `chat_id`.

- **Sliding window:** Each `add_message()` cancels the current `asyncio.Task` and schedules a fresh one for `DEBOUNCE_DELAY_SECONDS` (default 90s)
- **On fire:** Buffer is popped, messages sorted by `date` ASC, `callback(chat_id, messages)` awaited
- **`flush_all()`:** Force-dispatches all pending buffers on graceful shutdown
- **Zero unhandled exceptions:** Callback errors are caught and logged; task cancellation uses explicit `try/except CancelledError`

### 7.4 Backfill (`backfill.py`)
`fetch_24h_backfill(client, entity) -> List[dict]`

- Cutoff = `now(UTC) - 24h`
- `client.iter_messages(entity, offset_date=cutoff, reverse=True)` — chronological order
- Safety filter: drops any message with `date < cutoff` (edge cases)
- Returns `[{index, label "[N]", id, text, date, timestamp, has_media}, …]`

### 7.5 Listener (`listener.py`)
`TelegramIngestionService`:

| Handler | Trigger | Routing |
|---|---|---|
| `_on_new_message` | `events.NewMessage` | Main feed vs subchannel |
| `_on_edited_message` | `events.MessageEdited` | `is_edit=True` flag set |

**Main Feed path (`chat_id == MAIN_FEED_CHANNEL_ID`):**
1. Reply-to stitching: fetches `reply_to_msg_id` parent text → prepends `[CONTEXT: …]`
2. Join-link scan: `t.me/+…` or `t.me/joinchat/…` → `asyncio.create_task(_reconcile_and_backfill(link))`
3. Emits `(chat_id, "main_feed", [msg_dict])` to payload handler

**Subchannel path:**
1. `pre_filter.should_filter_noise()` — drop if noise
2. `ChannelDebouncer.add_message()` — buffer with 90s window
3. On fire → emits `(chat_id, "subchannel", messages)` to payload handler

**`set_payload_handler(handler)`** — pluggable async callback for Phase 4 dispatcher.

> **⚠ Spec Gap:** Recursive reply fetching (depth ≤ 6) is not implemented — only single-level parent stitching exists. This affects deeply threaded announcements.
> **⚠ Spec Gap:** Auto-join via `ImportChatInviteRequest` is not implemented — `_reconcile_and_backfill` calls `get_entity()` which works for public channels but will fail on private invite links if not already a member.
> **⚠ Spec Gap:** 60-second TCP heartbeat job (DC keep-alive) is not implemented.

---

## 8. AI Extraction & Dispatch Pipeline (`app/ai/`)

### 8.1 Extraction Schemas (`ai/schemas.py`)

| Type | Values |
|---|---|
| `ActionType` | `CREATE_EVENT`, `EXTEND_DEADLINE`, `RESCHEDULE_EVENT`, `REPLACE_FORM`, `STATUS_NOTE`, `NO_ACTION` |
| `DriveType` | `FTE`, `INTERNSHIP`, `COMBINED` |
| `AudienceScope` | `COHORT`, `INDIVIDUAL` |

`ExtractedEvent` fields: `action`, `role_name`, `drive_type`, `audience_scope`, `event_type`, `title`, `action_link`, `start_time`, `end_time`, `status_note`

> **⚠ Schema Divergence vs MASTER_SPEC §4:** The current `ExtractedEvent` is a simplified Phase 4 schema. The spec's canonical `PlacementExtractionPayload` requires: `is_actionable`, `roles[]` (with `lifecycle_note`), `events[].event_category` (RESUME/IRIS/FORM/OA/GD/…), `events[].round_number`, `events[].is_form_replacement`, `events[].venue`. The extractor and dispatcher need a full schema migration to conform.

### 8.2 Prompt Builder (`ai/prompts.py`)
`build_system_prompt(context: dict) -> str`

Injections per call:
- **Current UTC timestamp** (formatted as day/date/time string)
- **Company schedule state block** — event titles, types, deadlines, statuses
- **6 operational rules** — INDIVIDUAL scope, REPLACE_FORM, EXTEND_DEADLINE, RESCHEDULE, STATUS_NOTE, NO_ACTION
- **Verbatim JSON schema** — prevents hallucinated field names

### 8.3 Extractor (`ai/extractor.py`)
`extract_placement_actions(raw_text, current_context) -> LLMExtractionResult`

- `response_format={"type": "json_object"}` — forces JSON output
- `temperature=0.05` — near-deterministic
- `max_tokens=1024`
- Falls back to `LLMExtractionResult()` (empty) on API error, `ValidationError`, or empty response — **never raises**

### 8.4 Dispatcher (`ai/dispatcher.py`)
`process_and_dispatch(chat_id, context_header, messages, db)`

**5-step pipeline:**
1. Normalize text block from message list
2. Resolve `Company` by `subchannel_id == chat_id`
3. Build schedule context → inject into prompt
4. Call `extract_placement_actions()`; fallback name resolution if subchannel unmapped
5. Execute state machine per action (drop `INDIVIDUAL` scope first):

| Action | CRUD Operation |
|---|---|
| `CREATE_EVENT` | `crud.create_custom_event()` |
| `EXTEND_DEADLINE` | `crud.extend_event_deadline()` on best-match event |
| `RESCHEDULE_EVENT` | `crud.extend_event_deadline()` (treated as deadline update) |
| `REPLACE_FORM` | `crud.replace_form_event()` → version+1, status→PENDING |
| `STATUS_NOTE` | `UPDATE role_tracks SET latest_status_note=…` |
| `NO_ACTION` | No-op |

### 8.5 Lifespan Wiring (`app/main.py`)
```python
async def _payload_handler(chat_id, context_header, messages):
    db = AsyncSessionLocal()
    try:
        await dispatcher.process_and_dispatch(chat_id, context_header, messages, db)
    finally:
        await db.close()

await tg_service.set_payload_handler(_payload_handler)
```
Each handler call opens an **isolated `AsyncSession`** — decoupled from HTTP request lifecycle.

---

## 9. Lifespan Sequence (`app/main.py`)

```
Startup
  │
  ├─ 1. create_all() → Bootstrap DB schema (SQLite dev / PostgreSQL prod)
  │
  ├─ 2. [if TELEGRAM_API_ID != 0]
  │      ├─ create_telegram_client() → StringSession
  │      ├─ client.start()
  │      ├─ TelegramIngestionService.register_handlers()
  │      ├─ set_payload_handler(_payload_handler → dispatcher)
  │      └─ asyncio.create_task(client.run_until_disconnected())
  │
  └─ yield  ←── FastAPI serves requests

Shutdown
  ├─ tg_service.shutdown() → flush_all() debounce buffers
  ├─ task.cancel() + suppress(CancelledError)
  ├─ client.disconnect()
  └─ engine.dispose()
```

---

## 10. CORS & Networking

```python
CORSMiddleware(
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

- Bound on `0.0.0.0:8080` locally
- Exposed publicly via `cloudflared tunnel --url http://localhost:8080`
- HTTPS termination at Cloudflare edge — satisfies Android API 28+ cleartext restrictions

---

## 11. Verification Scripts

All scripts are self-contained and require no live credentials or running server:

| Script | Coverage | DB |
|---|---|---|
| `verify_phase1.py` | ORM model instantiation, all 4 `display_state` branches, Pydantic schema round-trip | None |
| `verify_phase2.py` | CRUD mutations, drive-type viewport filtering, form versioning, deadline extension/recovery | In-memory SQLite |
| `verify_phase3.py` | Pre-filter DROP/KEEP cases, debounce sliding window, edit event propagation, 24h backfill, reply-to stitching, payload handler dispatch | Mock objects |
| `verify_phase4.py` | LLM schema validation, INDIVIDUAL guard, REPLACE_FORM versioning, EXTEND_DEADLINE recovery, prompt context injection | In-memory SQLite |

Run any phase independently:
```bash
python verify_phase1.py
python verify_phase2.py
python verify_phase3.py
python verify_phase4.py
```

---

## 12. Immediate Backlog (Spec Conformance Gaps)

### P0 — Blocking Mobile Client

| # | Item | File(s) | Detail |
|---|---|---|---|
| 1 | **Implement `/triage` router** | `app/routers/triage.py` | `GET /triage` returns `[]` or `List[CompanyResponse]` where `tracking_mode=SILENT_TRACK`. `POST /triage/{id}/accept` calls `update_company_tracking(ELIGIBLE)`. Eliminates current HTTP 404. |

### P1 — Schema Conformance

| # | Item | File(s) | Detail |
|---|---|---|---|
| 2 | **Add `environment_type` to `Company`** | `models.py`, migration | `VARCHAR(10) DEFAULT 'MAIN'` — enables `?env=MAIN/TEST` filter (FR1) |
| 3 | **Add `round_number` to `TimelineEvent`** | `models.py`, migration | `INTEGER DEFAULT 1` — required for OA/Interview round state machine |
| 4 | **Add `has_attachments` to `TimelineEvent`** | `models.py`, migration | `BOOLEAN DEFAULT FALSE` — paperclip indicator in mobile UI |
| 5 | **Migrate `main_feed_msg_id` → array** | `models.py` | `BIGINT[]` in PostgreSQL; thread stitching requires tracking multiple message IDs |
| 6 | **Unify `event_type` enum** | `models.py`, `ai/schemas.py` | Align `IRIS_DEADLINE/OA_WINDOW` naming with spec's `IRIS/OA/FORM/GD/RESUME` |
| 7 | **Per-role `tracking_mode` on `RoleTrack`** | `models.py`, `crud.py` | Spec places tracking state at role level, not company level |
| 8 | **Full LLM schema migration** | `ai/schemas.py`, `ai/prompts.py`, `ai/dispatcher.py` | Adopt `PlacementExtractionPayload` (§4 of MASTER_SPEC): `is_actionable`, `roles[]`, `events[].is_form_replacement`, `events[].round_number` |

### P2 — Pipeline Completeness

| # | Item | File(s) | Detail |
|---|---|---|---|
| 9 | **Recursive reply stitching (depth ≤ 6)** | `telegram/listener.py` | Current impl fetches only 1 level of `reply_to_msg_id` |
| 10 | **Auto-join private channels** | `telegram/listener.py` | `_reconcile_and_backfill` needs `ImportChatInviteRequest` for `t.me/+` private links |
| 11 | **60s TCP heartbeat** | `app/main.py` | Background task pinging Telegram DC to reset half-open connections |
| 12 | **`system_configs` table & profile API** | `models.py`, new router | `GET/PUT /system-config` — student name, roll number, feed IDs for LLM defaulter filter |
| 13 | **FCM push notification worker** | `app/workers/notifier.py` | 60s polling: `PENDING` events ±2h or ±30min from `start_time` → FCM dispatch |
| 14 | **Defaulter list LLM filter** | `ai/prompts.py`, `ai/extractor.py` | Inject `system_configs.roll_number` into prompt; drop if user not in defaulter list |

### P3 — Deployment

| # | Item | File(s) | Detail |
|---|---|---|---|
| 15 | **`Dockerfile`** | `Dockerfile` | Multi-stage build; Uvicorn entry on port 8080 |
| 16 | **`docker-compose.yml`** | `docker-compose.yml` | Services: `api` + `postgres:16`; named volume for DB persistence |
| 17 | **Alembic migrations** | `alembic/` | Replace `create_all()` lifespan call with versioned migration runner |
| 18 | **Switch `DATABASE_URL` to PostgreSQL** | `.env` (prod) | `asyncpg` driver; `BIGINT[]` array types only available in PostgreSQL |

---

## 13. Git Log (Recent)

```
6de368c feat: Phase 1-4 complete — untrack __pycache__
4bda32f feat: complete Phase 4 LLM extraction pipeline and state-machine dispatcher
3513aa8 feat: complete Phase 3 Telegram ingestion engine with passing tests
8dbf50d feat: complete Phase 2 CRUD operations and REST API routers with passing tests
5f99623 chore: add root .gitignore
b45a9f6 feat: initialize placement-engine backend with Phase 1 models and schemas
```

---

## 14. Server Startup Command

```bash
# Activate venv
.\venv\Scripts\Activate.ps1

# Start server
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

# Expose via Cloudflare tunnel (separate terminal)
cloudflared tunnel --url http://localhost:8080
```

> Swagger UI available at `http://localhost:8080/docs` during development.
