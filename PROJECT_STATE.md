# PROJECT_STATE.md — Placement Engine Backend
**Last Updated:** 2026-10-20 | **Version:** 1.0.0 (Pre-Interview State Engine Complete) | **Branch:** `main`

> Single source of truth for the backend's operational status, architecture, and immediate backlog. Cross-referenced against `MASTER_SPEC.md` and `PRODUCT REQUIREMENTS DOCUMENT.md`.

---

## 0. Tribal Knowledge & Critical Engineering Invariants

> [!IMPORTANT]
> The following rules were forged from real-world runtime edge cases during live campus testing. Every engineer and AI agent operating in this repository **must** adhere strictly to these invariants:

1. **Timezone Representation (Zero-Dependency Native UTC+05:30 IST)**:
   - **Rule:** Timezones MUST be defined using standard library `datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")`.
   - **Why:** On Windows machines, `zoneinfo.ZoneInfo("Asia/Kolkata")` crashes fatally with `ZoneInfoNotFoundError: 'No time zone found with key Asia/Kolkata'` if the Python environment lacks the `tzdata` package. Because Indian Standard Time does not observe Daylight Saving Time (strictly fixed at UTC+05:30 year-round), the native `datetime.timezone` constructor provides 100% deterministic, cross-platform stability with zero external dependency risks.
   - **Enforcement:** Enforced in `app/ai/prompts.py`, `app/ai/schemas.py`, and `app/ai/rule_extractor.py`. In addition, `tzdata>=2024.1` is pinned defensively in `requirements.txt`.

2. **Groq AI Extraction Headroom (`max_tokens=4096`) & Dual-Tier Fallback Architecture**:
   - **Rule:** Both the primary and fallback LLM completions in `app/ai/extractor.py` MUST specify `max_tokens=4096` (never default or 1024), and extraction MUST follow a multi-tier fallback hierarchy.
   - **Why:** Complex campus announcements containing multiple concurrent actions or long eligibility updates produce large JSON payloads. Setting `max_tokens=1024` or lower causes Groq to truncate generation mid-stream, tripping JSON parser errors (`EOF while parsing a string` / `json_validate_failed`).
   - **Multi-Tier Fallback Structure:**
     1. **Tier 1 (Structured Groq Inference):** `temperature=0.0`, `response_format={"type": "json_object"}` with strict JSON formatting constraints in `build_system_prompt()`.
     2. **Tier 2 (In-Flight Fallback Completion):** Triggered on `json_validate_failed` or `APIError`. Retries without schema constraints, strips markdown code fences (` ```json `), and extracts the first valid JSON substring via regex.
     3. **Tier 3 (Deterministic Regex Fallback — `rule_extractor.py`):** If the LLM produces 0 actions or fails inference for messages containing unambiguous temporal keywords ("tomorrow 9pm", "today by 11:59 PM", "OA window"), regex patterns extract the event type and normalize IST timestamps into structured events.

3. **Test Feed Isolation & In-Memory SQLite Requirement (`:memory:`)**:
   - **Rule:** All test verification suites (`verify_phase1.py` through `verify_phase4.py`, `verify_triage.py`, `verify_settings.py`) MUST strictly bind to isolated in-memory databases (`sqlite+aiosqlite:///:memory:`) and override `app.database.engine`.
   - **Why:** Running tests against local disk files (`dev.db`) causes test fixtures to pollute the real `MAIN` feed environment with mock companies (`FallbackTestCorp`, `Acme Corp`), corrupting state for live Telegram listeners and mobile clients.
   - **Enforcement:** All database-touching verification suites bind `:memory:`. In addition, `scripts/clean_test_data.py` exists as a production hygiene utility to purge any stray test records from `MAIN`.

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
├── verify_triage.py              # Triage Hub & role tracking regression suite
├── verify_settings.py            # Settings API & SystemConfig regression suite
├── scripts/
│   └── test_live_ingestion.py    # Non-interactive live preflight & safety audit
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
    │   ├── events.py             # /events endpoints
    │   ├── triage.py             # /triage staging queue & action endpoints
    │   └── settings.py           # /settings user profile & feed configuration
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
        ├── client.py             # AsyncOpenAI factory (cached singleton)
        ├── schemas.py            # LLM extraction Pydantic models (relaxed defaults)
        ├── prompts.py            # System prompt builder (UTC + context injection)
        ├── extractor.py          # LLM inference + ValidationError fallback + timeout
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
| `environment_type` | String(16) | `MAIN` \| `TEST` (default `MAIN`) |
| `main_feed_msg_id` | BigInteger | Single-ID current impl (spec calls for array) |
| `subchannel_id` | BigInteger UNIQUE | Telegram channel ID for routing |
| `tracking_mode` | String(32) | `ELIGIBLE` \| `SILENT_TRACK` \| `IGNORED` |
| `is_active` | Boolean | Soft-delete flag |
| `is_custom` | Boolean | Manual entry vs Telegram-discovered |
| `created_at` | DateTime(tz) | UTC default |

**Relationships:** `role_tracks` (cascade delete), `timeline_events` (cascade delete)

> **⚠ Schema Gap vs MASTER_SPEC:** `main_feed_msg_id` is a single `BigInteger`. The spec defines `main_feed_msg_ids BIGINT[]` (array) for multi-message thread stitching. Needs migration when switching to PostgreSQL.

### 5.2 `RoleTrack`
| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `company_id` | FK → companies | CASCADE DELETE |
| `role_name` | String(255) | e.g. `DAA`, `BTSA`, `Software Engineer` |
| `drive_type` | String(32) | `FTE` \| `INTERNSHIP` \| `COMBINED` |
| `ctc_stipend_info` | Text | Package string |
| `tracking_mode` | String(32) | `ELIGIBLE` \| `SILENT_TRACK` \| `IGNORED` |
| `is_triaged` | Boolean | `False` = pending in Triage Hub |
| `lifecycle_status` | String(64) | `APPLICATION_WINDOW` \| `INTERVIEWS` \| … |
| `latest_status_note` | Text | Non-temporal status (e.g. "Shortlists delayed") |
| `created_at` | DateTime(tz) | UTC default |
| `updated_at` | DateTime(tz) | Auto-updated on write |

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
| `is_deleted` | Boolean | Soft-delete flag (`default=False`) |
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
| Method | Path | Request Body / Params | Response |
|---|---|---|---|
| `POST` | `/companies/` | `CompanyCreate` (accepts `name`, `company_name`, `target_role`, `role_name`, `drive_type`, `ctc_stipend_info`, `environment_type="MAIN"`) | `CompanyResponse` (201) — atomically creates parent company and default `RoleTrack` (`is_triaged=True`, `tracking_mode="ELIGIBLE"`) |
| `GET` | `/companies/` | `?tracking_mode=&env=MAIN\|TEST` | `List[CompanyResponse]` (filters `is_active == True`, serializes both `name` and `company_name`) |
| `GET` | `/companies/{id}/events` | — | `List[TimelineEventResponse]` (chronological, filters `is_deleted == False`, returns 404 if company does not exist) |
| `PATCH` | `/companies/{id}/tracking` | `ToggleTrackingPayload` | `CompanyResponse` (cascades `tracking_mode` to child `RoleTrack`s) |
| `POST` | `/companies/{id}/tracks` | `RoleTrackCreate` | `RoleTrackResponse` (201) |
| `DELETE` | `/companies/{id}` | — | `{"status": "deleted", "id": id}` (soft-deletes company `is_active=False` & cascades `is_deleted=True` to events) |

#### Timeline Events — `/events`
| Method | Path | Request Body / Params | Response |
|---|---|---|---|
| `GET` | `/events/timeline` | `?drive_type=FTE&env=MAIN\|TEST` | `List[TimelineEventResponse]` (LEFT OUTER JOIN on `RoleTrack` so company-level events are preserved; filters `Company.is_active == True` and `TimelineEvent.is_deleted == False`; serializes both `display_state` and `status`) |
| `POST` | `/events/` | `TimelineEventCreate` | `TimelineEventResponse` (201) |
| `PATCH` | `/events/{id}/complete` | `?completed=true` | `TimelineEventResponse` |
| `POST` | `/events/{id}/uncomplete` | — (alias `/reopen`) | `TimelineEventResponse` (resets `user_status=PENDING`, `completed_at=None`) |
| `PATCH` | `/events/{id}/extend` | `ExtendDeadlinePayload` | `TimelineEventResponse` |
| `POST` | `/events/companies/{id}/replace-form` | `ReplaceFormPayload` | `TimelineEventResponse` (201) |
| `DELETE` | `/events/{id}` | — | `{"status": "deleted", "id": id}` (soft-deletes event `is_deleted=True`) |

#### Triage Hub — `/triage`
| Method | Path | Request Body / Params | Response |
|---|---|---|---|
| `GET` | `/triage/pending` | `?env=MAIN\|TEST` | `List[TriageItemResponse]` (`is_triaged == False`, newest first, includes `environment_type`) |
| `GET` | `/triage` | `?env=MAIN\|TEST` | Alias to `/triage/pending` |
| `POST` | `/triage/{id}` | `TriageActionPayload` | `TriageItemResponse` (sets `tracking_mode` & `is_triaged=True`, cascades `ELIGIBLE` to parent `Company`) |
| `POST` | `/triage/{id}/accept` | — | `TriageItemResponse` (convenience shortcut for `ELIGIBLE`, cascades to parent `Company`) |
| `POST` | `/triage/{id}/ignore` | — | `TriageItemResponse` (convenience shortcut for `IGNORED`) |

#### System Settings — `/settings`
| Method | Path | Request Body | Response |
|---|---|---|---|
| `GET` | `/settings` | — | `SystemConfigResponse` (404 if uninitialized) |
| `PUT` | `/settings` | `SystemConfigUpdate` | `SystemConfigResponse` (partial upsert) |

### 6.2 Missing Routes (Remaining Backlog)

| Method | Path | Status | Priority |
|---|---|---|---|
| `GET` | `/companies/{id}` | ❌ Not implemented | P1 |
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
- Fetches recent messages backwards from now with defensive safety cap (`limit=200`), filtering `msg_date >= cutoff` (resolves Telethon `offset_date` + `reverse=True` bug).
- Returns chronological payload matching live message contract: `[{index, label "[N]", id, text, raw_text, date, timestamp, has_media}, …]`

### 7.5 Listener (`listener.py`) & Two-Tier Ingestion
`TelegramIngestionService`:

- **Feed ID Caching & Task Tracking:** Caches `self.root_feed_ids` in memory to eliminate DB queries per event; tracks spawned `asyncio.Task` references in `self._background_tasks` with error logging callbacks.

| Tier | Feeds / Target | Routing Logic |
|---|---|---|
| **Tier 1 (Root Feeds)** | Configured `main_feed_id` or `test_feed_id` | 1. Reply-to stitching: parent context merged.<br>2. Invite link scan (`t.me/+...`, `t.me/joinchat/...`, `t.me/<username>`): auto-joins (`ImportChatInviteRequest` / `JoinChannelRequest`), links `Company` via `crud.link_or_create_subchannel_company` (`SILENT_TRACK`, `is_triaged=False`, stamped with parent `environment_type`), adds to `active_subchannel_ids`, and triggers 24h backfill.<br>3. Direct announcements (no invite): routed through `ChannelDebouncer` to Groq. |
| **Tier 2 (Sub-channels)** | Any `chat_id` in `active_subchannel_ids` | 1. Noise gate (`pre_filter.should_filter_noise`).<br>2. Sliding debouncer (`ChannelDebouncer`, 90s).<br>3. Dispatches consolidated payload to Groq (`openai/gpt-oss-20b`) via `dispatcher.process_and_dispatch`. |

**Read-Only Safety & Feed Isolation Policy:**
- **Zero Outbound/Destructive Operations:** Strictly an incoming event listener (`events.NewMessage`, `events.MessageEdited`). No `send_message`, `delete_messages`, `edit_message`, or `LeaveChannelRequest`. Auto-joining subchannels via `ImportChatInviteRequest` / `JoinChannelRequest` is the only permitted Telegram action.
- **Feed Isolation:** Automatically drops private DMs (`event.is_private`) and unmonitored channels.
- **FastAPI Lifespan Integration (`app/main.py`):** Non-interactive startup using `await tg_client.connect()` and `await tg_client.is_user_authorized()`, bootstrapping `await tg_service.initialize()`, wiring dispatcher callback, logging `[TELEGRAM] Ingestion service active. Root feeds: Main=<id>, Test=<id>`, and running listener in background task `asyncio.create_task(tg_client.run_until_disconnected())`. On shutdown, debouncers and client are cleanly disconnected.

> **⚠ Spec Gap:** Recursive reply fetching (depth ≤ 6) is not implemented — only single-level parent stitching exists. This affects deeply threaded announcements.
> **⚠ Spec Gap:** 60-second TCP heartbeat job (DC keep-alive) is not implemented.

---

## 8. AI Extraction & Dispatch Pipeline (`app/ai/`)

### 8.1 Extraction Schemas (`ai/schemas.py`)

| Type | Values |
|---|---|
| `ActionType` | `CREATE_EVENT`, `EXTEND_DEADLINE`, `RESCHEDULE_EVENT`, `REPLACE_FORM`, `STATUS_NOTE`, `NO_ACTION` |
| `DriveType` | `FTE`, `INTERNSHIP`, `COMBINED` (default `"FTE"`) |
| `AudienceScope` | `COHORT`, `INDIVIDUAL` (default `"COHORT"`) |

`ExtractedEvent` fields: `action`, `company_name` (optional), `role_name` (optional), `drive_type` (default `"FTE"`), `audience_scope` (default `"COHORT"`), `event_type`, `title` (optional, falls back to `event_type`), `action_link`, `start_time`, `end_time`, `status_note`.

> **⚠ Schema Divergence vs MASTER_SPEC §4:** The current `ExtractedEvent` is a simplified Phase 4 schema (with Phase 1 null-safety relaxations). The spec's canonical `PlacementExtractionPayload` requires: `is_actionable`, `roles[]` (with `lifecycle_note`), `events[].event_category` (RESUME/IRIS/FORM/OA/GD/…), `events[].round_number`, `events[].is_form_replacement`, `events[].venue`.

### 8.2 Prompt Builder (`ai/prompts.py`)
`build_system_prompt(context: dict) -> str`

Injections per call:
- **Groq JSON Schema Compliance Header:** Strict formatting constraints requiring pure JSON output without markdown fences.
- **Current IST Timestamp & Anchoring:** Dynamic Indian Standard Time timestamp generated via native `_IST = timezone(timedelta(hours=5, minutes=30), name="IST")`.
- **Relative Date Resolution Rules:** Explicit resolution rules converting "today" (calendar date in IST), "tonight" (23:59:59 IST), "tomorrow", and upcoming weekday names relative to today.
- **Company Schedule State Block:** Current event titles, types, deadlines, and statuses to allow resolving "same timings", "shifted by 2 hours", etc.
- **Strict Operational Rules:** Action selection criteria for `CREATE_EVENT`, `EXTEND_DEADLINE`, `RESCHEDULE_EVENT`, `REPLACE_FORM`, `STATUS_NOTE`, and `NO_ACTION`.
- **Verbatim JSON Schema:** Injected schema template preventing hallucinated field names.

### 8.3 Extractor (`ai/extractor.py`) & Fallback Extractor (`ai/rule_extractor.py`)
`extract_placement_actions(raw_text, current_context) -> LLMExtractionResult`

- Cached module-level `AsyncOpenAI` singleton via `get_ai_client()` for connection reuse
- `timeout=30.0` on completions call
- `response_format={"type": "json_object"}` — forces JSON output
- `temperature=0.0` — deterministic extraction
- `max_tokens=4096` — ensures full token headroom avoiding mid-JSON truncation (`EOF while parsing a string`)
- **Dual-Tier Fallback Architecture:**
  1. *In-flight Fallback:* If Groq returns 400 `json_validate_failed` or invalid JSON, re-requests without schema enforcement and extracts JSON with code-fence stripping.
  2. *Secondary Deterministic Regex Fallback (`rule_extractor.py`):* If the LLM produces 0 actions or fails inference for messages containing clear recruitment temporal keywords ("tomorrow 9pm", "today by 11:59 PM", "OA window 8-10am"), deterministic regex patterns extract event types and IST timestamps directly into structured actions.
- Falls back to `LLMExtractionResult()` (empty) on unrecoverable error — **never raises**

### 8.4 Dispatcher & Pre-Interview State Engine (`ai/dispatcher.py`)
`process_and_dispatch(chat_id, context_header, messages, db)`

**Fully Hardened 5-Step Pipeline:**
1. Normalize text block from message list.
2. Resolve `Company` by `subchannel_id == chat_id`, falling back to LLM-extracted name or subchannel mapping.
3. Build schedule context → inject into prompt.
4. Call `extract_placement_actions()` with automated fallback chain.
5. Execute pre-interview state machine mutations per action (silently dropping `INDIVIDUAL` scope):

| Action | State Machine Transition & Fallback |
|---|---|
| `CREATE_EVENT` | Normalizes start/end times (`_normalize_event_times`) and invokes `crud.create_custom_event()`. |
| `EXTEND_DEADLINE` | Queries `_find_matching_event` (filtering `is_deleted == False`). If found, updates deadline via `crud.extend_event_deadline()`. **Fallback:** If no prior event exists, falls back seamlessly to `CREATE_EVENT`. |
| `RESCHEDULE_EVENT` | Queries `_find_matching_event` (filtering `is_deleted == False`). If found, updates both `start_time` and `end_time` via `crud.reschedule_event()`. **Fallback:** If no prior event exists, falls back seamlessly to `CREATE_EVENT`. |
| `REPLACE_FORM` | If a prior form event exists and `action_link` is present, calls `crud.replace_form_event()` to increment `version = max_version + 1`, update `action_link`, set both `start_time` and `end_time` to `new_end_time`, and reset `user_status = "PENDING"`. **Fallback:** If no prior event exists, falls back to `CREATE_EVENT` with `event_type="FORM_SUBMISSION"`. |
| `STATUS_NOTE` | Criteria Relaxation / Re-Triage trigger: calls `crud.flag_for_retriage(db, target_track.id, note=extracted.status_note)`. Persists `latest_status_note` and resets `is_triaged = False` strictly if `tracking_mode == "SILENT_TRACK"`. |
| `NO_ACTION` | No-op. |

**Temporal Normalization Rules (`_normalize_event_times`):**
- *Live events* (`OA_WINDOW`, `PPT`, `INTERVIEW`): If only `start_time` is present, defaults `end_time = start_time + 2 hours`.
- *Deadlines* (`FORM_SUBMISSION`, `IRIS_DEADLINE`, `CUSTOM`): If only one timestamp is present, sets `start_time = end_time = timestamp`.
- *Start/End present:* Preserves exact bounds.

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
    allow_origin_regex=r"^https?://.*",
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

All scripts are self-contained and require no live credentials or running server (except `test_live_ingestion.py` which requires read-only session credentials):

| Script | Coverage | DB |
|---|---|---|
| `verify_phase1.py` | ORM model instantiation, all 4 `display_state` branches, Pydantic schema round-trip | None |
| `verify_phase2.py` | CRUD mutations, drive-type viewport filtering, form versioning, deadline recovery, HTTP router tests (`POST /companies/` mobile contract, `GET /companies/{id}/events` 200/404, timeline `?env=MAIN\|TEST` isolation, complete/uncomplete) | In-memory SQLite |
| `verify_phase3.py` | Pre-filter DROP/KEEP cases, debounce sliding window, edit event propagation, 24h backfill, reply-to stitching, payload handler dispatch | Mock objects |
| `verify_phase4.py` | LLM schema validation, INDIVIDUAL guard, REPLACE_FORM, EXTEND_DEADLINE, prompt context injection, dirty subchannel payloads (missing company/drive), e2e CREATE_EVENT, e2e STATUS_NOTE, RESCHEDULE_EVENT start/end updates | In-memory SQLite |
| `verify_triage.py` | RoleTrack defaults, pending triage items, re-triage criteria relaxation, isolated fixtures, Company tracking mode cascade assertion, `/triage/pending`, `/accept`, `/ignore`, 404 endpoints | In-memory SQLite |
| `verify_settings.py` | SystemConfig ORM, GET/PUT `/settings` endpoints, partial update upsert | In-memory SQLite |
| `scripts/test_live_ingestion.py` | Safe live preflight: Telethon auth session, Telegram entity resolution, Groq LLM check, read-only safety audit | Live API (Read-only) |

Run regression suites:
```bash
python verify_phase1.py
python verify_phase2.py
python verify_phase3.py
python verify_phase4.py
python verify_triage.py
python verify_settings.py
python scripts/test_live_ingestion.py
```

---

## 12. Immediate Backlog (Spec Conformance Gaps)

### P0 — Core Pre-Interview State Machine & Mobile Contract (All Resolved & Hardened)

| # | Item | File(s) | Status / Detail |
|---|---|---|---|
| 1 | **Implement `/triage` router** | `app/routers/triage.py` | ✅ **Resolved** — `GET /triage/pending`, `POST /triage/{id}`, `/accept`, `/ignore`, and conditional re-triage. |
| — | **Atomic Company Creation** | `app/crud.py`, `app/schemas.py` | ✅ **Resolved** — `POST /companies/` creates default `RoleTrack` atomically with role metadata. |
| — | **Enriched Timeline & Events** | `app/schemas.py`, `app/routers/companies.py` | ✅ **Resolved** — `TimelineEventResponse` enriched with `company_name`, `drive_type`, `status`; `GET /companies/{id}/events` implemented. |
| — | **Pre-Interview State Engine** | `app/ai/dispatcher.py`, `app/crud.py` | ✅ **Resolved & Hardened** — Canonical time normalization (live events +2h, deadlines same timestamp), non-existent event fallbacks (`EXTEND_DEADLINE`/`RESCHEDULE_EVENT`/`REPLACE_FORM` → `CREATE_EVENT`), form replacement versioning with PENDING reset, soft-delete filtering, and criteria relaxation re-triage via `crud.flag_for_retriage()`. |
| — | **Zero-Dependency IST Timezone** | `app/ai/prompts.py`, `app/ai/schemas.py` | ✅ **Resolved** — Native UTC+05:30 `timezone` representation eliminating Windows `tzdata` crashes. |
| — | **Dual-Tier AI Extraction & Fallbacks**| `app/ai/extractor.py`, `app/ai/rule_extractor.py` | ✅ **Resolved** — `max_tokens=4096`, in-flight fallback parser, and deterministic regex IST fallback parser. |
| — | **Database Isolation & Sanitization** | `verify_*.py`, `scripts/clean_test_data.py` | ✅ **Resolved** — Test suites isolated to `:memory:` SQLite; `dev.db` untracked from Git; clean test isolation verified. |

### P1 — Schema Conformance

| # | Item | File(s) | Detail |
|---|---|---|---|
| 2 | **Add `environment_type` to `Company`** | `models.py`, `schemas.py`, `crud.py` | ✅ **Resolved** — `VARCHAR(16) DEFAULT 'MAIN'` added to `Company` with `?env=MAIN/TEST` filtering across `/companies/`, `/events/timeline`, and `/triage/pending`. |
| 3 | **Add `round_number` to `TimelineEvent`** | `models.py`, migration | `INTEGER DEFAULT 1` — required for OA/Interview round state machine |
| 4 | **Add `has_attachments` to `TimelineEvent`** | `models.py`, migration | `BOOLEAN DEFAULT FALSE` — paperclip indicator in mobile UI |
| 5 | **Migrate `main_feed_msg_id` → array** | `models.py` | `BIGINT[]` in PostgreSQL; thread stitching requires tracking multiple message IDs |
| 6 | **Unify `event_type` enum** | `models.py`, `ai/schemas.py` | Align `IRIS_DEADLINE/OA_WINDOW` naming with spec's `IRIS/OA/FORM/GD/RESUME` |
| 7 | **Per-role `tracking_mode` on `RoleTrack`** | `models.py`, `crud.py` | ✅ **Resolved** — Migrated to `RoleTrack` with `is_triaged` & timeline query alignment. |
| 8 | **Full LLM schema migration** | `ai/schemas.py`, `ai/prompts.py`, `ai/dispatcher.py` | Phase 1 schema relaxed with optional defaults (`drive_type="FTE"`, `audience_scope="COHORT"`, nullable strings); full `PlacementExtractionPayload` (§4 of MASTER_SPEC) migration scheduled for PostgreSQL deployment. |

### P2 — Pipeline Completeness

| # | Item | File(s) | Detail |
|---|---|---|---|
| 9 | **Recursive reply stitching (depth ≤ 6)** | `telegram/listener.py` | Current impl fetches only 1 level of `reply_to_msg_id` |
| 10 | **Auto-join private channels** | `telegram/listener.py` | ✅ **Resolved** — Auto-joins via `ImportChatInviteRequest` (hashes) / `JoinChannelRequest` (usernames) and links SQLite `Company` record. |
| 11 | **60s TCP heartbeat** | `app/main.py` | Background task pinging Telegram DC to reset half-open connections |
| 12 | **`system_config` table & profile API** | `models.py`, `app/routers/settings.py` | ✅ **Resolved** — `GET /settings` and `PUT /settings` implemented with student profile and feed IDs. |
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
3533cb9 chore: strengthen gitignore rules
09cbaa5 chore(git): scrub dev.db from index, update .gitignore, add .env.example and production README
fbcd333 fix(tz): use cross-platform UTC+05:30 IST definition and add tzdata dependency
e850a9c feat(pre-interview): implement master spec time normalization, ist anchoring, form replacement, and test feed isolation
e9a894e feat(ecosystem): fallback extend to create, normalize event times, add ist regex fallback, and wire iris deadline frontend tag
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
