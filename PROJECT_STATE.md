# PROJECT_STATE.md — Placement Engine Backend
**Last Updated:** 2026-10-22 | **Version:** 1.0.0 — **Status: pre-production** (verified gaps in §0.2; remediation "Phase 0 — stop silent losses" in progress) | **Branch:** `feature/phase-0-stop-losses`

> Single source of truth for the backend's operational status, architecture, and immediate backlog. Cross-referenced against `MASTER_SPEC.md` and `PRODUCT REQUIREMENTS DOCUMENT.md`.
> **The code is the source of truth.** Every status claim in this file was re-verified against the code on 2026-10-22; intended-but-unbuilt behaviour belongs in `MASTER_SPEC.md`, not here.

---

## 0. Tribal Knowledge & Critical Engineering Invariants

> [!IMPORTANT]
> The following rules were forged from real-world runtime edge cases during live campus testing. Every engineer and AI agent operating in this repository **must** adhere strictly to these invariants:

1. **Timezone Representation (Zero-Dependency Native UTC+05:30 IST)**:
   - **Rule:** Timezones MUST be defined using standard library `datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")`.
   - **Why:** On Windows machines, `zoneinfo.ZoneInfo("Asia/Kolkata")` crashes fatally with `ZoneInfoNotFoundError: 'No time zone found with key Asia/Kolkata'` if the Python environment lacks the `tzdata` package. Because Indian Standard Time does not observe Daylight Saving Time (strictly fixed at UTC+05:30 year-round), the native `datetime.timezone` constructor provides 100% deterministic, cross-platform stability with zero external dependency risks.
   - **Enforcement:** Enforced in `app/ai/prompts.py`, `app/ai/schemas.py`, and `app/ai/rule_extractor.py`. In addition, `tzdata>=2024.1` is pinned defensively in `requirements.txt`.
   - **⚠ Storage gap (verified 2026-10-21):** SQLite's `DateTime(timezone=True)` drops the offset *without converting*. LLM/app writes persist IST wall-clock, `rule_extractor` writes persist UTC, and `display_state` reads everything back as UTC — so IST-written deadlines turn MISSED 5.5 h late. **✅ Fixed in Phase 0.2:** every datetime column now uses the `UTCDateTime` type (stored as UTC, read back as aware UTC), and API inputs must carry an offset (`AwareDatetime`). Times shown to the LLM are converted back to IST.

2. **Groq AI Extraction Headroom (`max_tokens=4096`) & Dual-Tier Fallback Architecture**:
   - **Rule:** Both the primary and fallback LLM completions in `app/ai/extractor.py` MUST specify `max_tokens=4096` (never default or 1024), and extraction MUST follow a multi-tier fallback hierarchy.
   - **Why:** Complex campus announcements containing multiple concurrent actions or long eligibility updates produce large JSON payloads. Setting `max_tokens=1024` or lower causes Groq to truncate generation mid-stream, tripping JSON parser errors (`EOF while parsing a string` / `json_validate_failed`).
   - **Multi-Tier Fallback Structure:**
     1. **Tier 1 (Structured Groq Inference):** `temperature=0.0`, `response_format={"type": "json_object"}` with strict JSON formatting constraints in `build_system_prompt()`.
     2. **Tier 2 (In-Flight Fallback Completion):** Triggered on `json_validate_failed` or `APIError`. Retries without schema constraints, strips markdown code fences (` ```json `), and extracts the first valid JSON substring via regex.
     3. **Tier 3 (Deterministic Regex Fallback — `rule_extractor.py`):** If the LLM produces 0 actions or fails inference for messages containing unambiguous temporal keywords ("tomorrow 9pm", "today by 11:59 PM", "OA window"), regex patterns extract the event type and normalize IST timestamps into structured events.
   - **⚠ Tier 3 is under revision — do not extend it (verified 2026-10-21).** It also fires when the LLM *validly* returns zero actions (the prompt asks for `{"actions": []}` on non-actionable text), and it was verified to create wrong events: "Round 2 of the OA is tomorrow at 5pm" → 02:00; "OA on 25th October at 5 PM" → today 17:00; a defaulter-list message → a FORM deadline (defeats PRD Workflow 6). Removal is scheduled for Phase 0.5, with failed batches retried instead. Unverified hypothesis: gpt-oss-20b is a reasoning model whose reasoning tokens count against `max_tokens`, which may be the real cause of the original truncation.

3. **Test Feed Isolation & In-Memory SQLite Requirement (`:memory:`)**:
   - **Rule:** All test verification suites (`verify_phase1.py` through `verify_phase4.py`, `verify_triage.py`, `verify_settings.py`) MUST strictly bind to isolated in-memory databases (`sqlite+aiosqlite:///:memory:`) and override `app.database.engine`.
   - **Why:** Running tests against local disk files (`dev.db`) causes test fixtures to pollute the real `MAIN` feed environment with mock companies (`FallbackTestCorp`, `Acme Corp`), corrupting state for live Telegram listeners and mobile clients.
   - **Enforcement:** Since Phase 0.2, every database-touching suite (`verify_phase2/3/4.py`, `verify_triage.py`, `verify_settings.py`, `verify_schema.py`) binds `:memory:`; `verify_phase3.py` was patched (it used to read the configured database through `_is_known_subchannel`). All suites also enable SQLite foreign keys and turn `NaiveDatetimeWarning` into an error.
   - **⚠ Do not run `scripts/clean_test_data.py` against real data:** it *hard-deletes* every `MAIN` company whose name merely contains `test`, `dummy`, `fallback`, `mock` or `acme` (e.g. "Latest updates…", Testbook, Acme Solar). Fix scheduled for Phase 0.6.

---

## 0.1 Tracking-Status Invariants (binding — set by the owner, 2026-10-22)

> [!IMPORTANT]
> These rules override `MASTER_SPEC.md` §3 where they differ (a matching MASTER_SPEC edit is pending owner approval). Implementation lands in Phases 0.3–0.5; until then the code does **not** yet enforce items 2, 3, 4, 5, 6 (note kinds) and 9.

1. **IGNORED is a terminal, human-only status.** No automated path (dispatcher, re-triage, linking, backfill, catch-up) may set or clear it on a company or role. Only the Companies-screen toggle — `PATCH /companies/{id}/tracking` — moves a company out of IGNORED. Setting IGNORED via the Triage "Discard" action is allowed (explicit user action).
2. A company is **effectively IGNORED** when its company-level mode is IGNORED or every one of its role tracks is IGNORED. Discarding the last role in Triage syncs the company to IGNORED. A multi-role company with any role still tracked keeps ingesting.
3. **Ingestion gate:** messages from an effectively-IGNORED or soft-deleted company's channel are dropped at the router — no pre-filter, no LLM call, no backfill/catch-up reads, no ledger rows. Main-feed posts attributed to such a company apply no mutations. The dispatcher re-checks before every write.
4. **Un-ignore** (via the PATCH above) resumes ingestion with a bounded 24-hour backfill — not a replay of everything missed.
5. Triage endpoints **refuse to change an IGNORED role** (HTTP 409).
6. **Criteria relaxation applies only to SILENT_TRACK.** Status notes are classified `CRITERIA_RELAXATION` or `GENERAL`. Only `CRITERIA_RELAXATION` on a SILENT_TRACK role re-opens it in Triage (`is_triaged=false`) as a proposal; `tracking_mode` is never changed automatically (no auto-promotion). `GENERAL` notes only update the note text. For ELIGIBLE roles a note never re-opens Triage, while timeline events, cards and (future) push notifications continue normally.
7. **No eligibility matching:** CGPA/branch are never collected or sent to the LLM; the model only detects *that* criteria were relaxed.
8. **SILENT_TRACK:** ingestion continues; nothing appears on the Timeline; no reminders (future) — only the relaxation proposal.
9. An explicit company-level `PATCH /companies/{id}/tracking` marks all of that company's roles triaged (`is_triaged=true`).

---

## 0.2 Known Gaps (verified review, 2026-10-21)

Verified by reading the code; items marked † were also reproduced by executing the pure function or an in-memory database. "Fix" refers to the remediation plan in §12.

| ID | Gap | Evidence | Fix |
|---|---|---|---|
| C1 | No reminder path end-to-end: no backend scheduler or push code; the app never schedules local alarms and never sends its push token. | `command-center/src/services/notifications.ts` (`scheduleEventReminders` unused) | Next phase |
| C2 † | Pre-filter drops real announcements: "ok/okay/noted/thanks" *anywhere* → drop; plurals, "tomorrow/tonight", "GD", "venue" aren't signals. 8 of 9 realistic samples dropped. | `app/telegram/pre_filter.py:53-102` | 0.4 |
| C3 | A root-feed post containing an invite link is only joined — its own text (roles, CTC, deadlines) never reaches the LLM; the company gets the channel title, "Default Role", FTE. | `app/telegram/listener.py:285-294`, `app/crud.py:530-551` | 0.4 |
| C4 | Downtime or LLM failure = permanent loss: no catch-up on reconnect, in-memory debounce buffers, no retry queue. | `app/telegram/client.py:41-47`, `app/ai/extractor.py:152-159` | Downtime ✅ 0.3 (ledger catch-up + retries); LLM failures retryable from 0.5 (the extractor still swallows errors) |
| C5 † | Regex fallback invents wrong events and fires on valid empty LLM results (see Tribal Knowledge #2). | `app/ai/extractor.py:66-77`, `app/ai/rule_extractor.py` | 0.5 |
| C6 | Auto-join resolves any `t.me/<name>`: a person/bot username creates a company plus a 24 h backfill of the private DM history (sent to the LLM); permalinks join foreign channels; the private-hash fallback re-resolves the hash as a username. | `app/telegram/listener.py:40-47,363-366,376,425-435` | 0.4 |
| C7 | No authentication on any route; CORS reflects any origin with credentials. | `app/main.py:145-151` | 0.6 |
| H1 † | Mixed IST/UTC wall-clock storage in SQLite (see Tribal Knowledge #1). | `app/models.py:145-158` | ✅ 0.2 |
| H2 | Message post times never reach the LLM; relative dates resolve against processing time (wrong for backfill, midnight, catch-up). | `app/ai/dispatcher.py:407-417`, `app/ai/prompts.py:90,130-137` | 0.5 |
| H3 | Duplicates by construction: edits re-extracted as new text (no message provenance), invite re-posts re-run the 24 h backfill, contradictory prompt rules turn reminders into CREATE, no dedupe key, overlapping dispatches per chat. | `listener.py:415-421`, `prompts.py:94-96` vs `115-118`, `crud.py:292-318`, `debouncer.py:111-119` | Re-posted invites no longer re-process ✅ 0.3 (ledger); rest 0.4–0.5 |
| H4 | Weak update targeting: match = latest event of the same type; EXTEND moves only `end_time`; REPLACE_FORM leaves the old form visible; deleted events are fed to the LLM and resurrect; no CANCELLED/TBA. | `dispatcher.py:57-62,108-128`, `crud.py:347-400` | 0.5 (partial) |
| H5 | Entity/env isolation: exact-name match; name lookups ignore `environment_type`; `normalized_name` globally unique; `link_or_create_subchannel_company` overwrites `environment_type`; per-action names override the subchannel's company; main-feed calls get no schedule context. | `dispatcher.py:35-42,420-428,505-514`, `crud.py:511-525` | Uniqueness ✅ 0.2; lookups 0.5 |
| H6 | Profile unused: INDIVIDUAL messages dropped even when addressed to the user; defaulter, slot and shortlist logic unimplemented. | `dispatcher.py:497-503` | Next phase |
| H7 | On PostgreSQL the suppressed `ALTER`s abort the `create_all` transaction, whose COMMIT then rolls back (schema never created); `BOOLEAN DEFAULT 0` is invalid on PG. | `app/main.py:31-40` | ✅ 0.2 (Alembic; live-PG run is the first Phase 1 step) |
| H8 | Telethon runs inside the API process: >1 worker or two hosts on one session → `AUTH_KEY_DUPLICATED`; the listener task is unsupervised; `/health` always reports healthy. | `app/main.py:48-113,159-161` | ✅ 0.3 (separate worker process; liveness in `worker_status`; status endpoint 0.6) |
| H9 | The README Dockerfile copies `.env`, the session file and `dev.db` into the image (no `.dockerignore`); `login_telegram.py` prints the session string. | `README.md` (Docker), `scripts/login_telegram.py:66-69` | 0.6, Phase 1 |
| M1–M7 | No per-action rollback; every STATUS_NOTE re-triages; the company toggle leaves `is_triaged=false`; the debouncer has no max-wait; Add Stage silently defaults to now+24 h; substring hard-delete script; documentation drift (this revision). | review 2026-10-21 | 0.2–0.7 |

---

## 1. Repository Identity

| Field | Value |
|---|---|
| **Project Name** | Placement Command Center — Backend Engine |
| **Repo** | `scn5/placement-engine` |
| **Runtime** | Local venv: Python 3.14.3, FastAPI 0.141, Uvicorn 0.54 (unpinned — see §3). Production image: **Python 3.12** — code must stay 3.12-compatible |
| **Local Bind** | `0.0.0.0:8000` (README / CLAUDE.md commands; earlier sessions used `8080`) |
| **Processes** | Since Phase 0.3: the **API** (`uvicorn app.main:app`, no Telegram code) and the **ingestion worker** (`python -m app.worker`) — exactly **one** worker per Telegram session |
| **Public Exposure** | Today: Cloudflare Quick Tunnel (URL changes on every restart). Planned: named tunnel on an owned domain + shared API key |
| **DB (dev)** | SQLite via `aiosqlite` (`./dev.db`, git-ignored, never committed). Recreated empty on 2026-10-23 by `alembic upgrade head` (Phase 0.2 cold start). The previous file — TEST data only (4 companies, 4 "Default Role" tracks, 9 events) — is kept as `dev.db.pre-phase0.bak` (git-ignored) |
| **DB (target)** | Chosen at VM provisioning: PostgreSQL (OCI A1 ARM) or SQLite (1 GB micro); schema kept portable |
| **LLM Endpoint** | `openai/gpt-oss-20b` via OpenAI-compatible base URL (Groq LPU), `json_object` response mode (not JSON-schema) |
| **Tenancy** | Strictly single-tenant, no multi-user scope. **No auth layer today** (gap C7); a single shared API key is planned (Phase 0.6) |

---

## 2. Source File Inventory

```
placement-engine/
├── .env                          # Local dev secrets (git-ignored)
├── .env.example                  # Safe credential template (committed)
├── .gitignore                    # Excludes venv/, *.env, __pycache__, *.log
├── requirements.txt              # Dependency lower bounds (no lock file yet)
├── alembic.ini, alembic/         # Schema migrations (env.py reads DATABASE_URL; versions/0001_baseline_schema.py)
├── AGENTS.md, README.md          # Agent rules / setup guide (MASTER_SPEC.md and the PRD live at the workspace root, outside this repo)
├── placement_session.session     # Live Telethon file session (git-ignored; used when TELEGRAM_SESSION_STRING is empty)
├── verify_phase1.py              # Schema & model smoke tests (no DB required)
├── verify_phase2.py              # CRUD & router tests (in-memory SQLite)
├── verify_phase3.py              # Telegram ingestion mock suite
├── verify_phase4.py              # LLM extraction & dispatch mock suite
├── verify_triage.py              # Triage Hub & role tracking regression suite
├── verify_settings.py            # Settings API & SystemConfig regression suite
├── verify_schema.py              # Alembic baseline, UTC storage & constraint suite (Phase 0.2)
├── verify_worker.py              # Ingestion worker, ledger, catch-up & retry suite (Phase 0.3)
├── scripts/
│   ├── test_live_ingestion.py    # Live preflight against the real session (read-only; owner-run only)
│   ├── login_telegram.py         # Interactive login → placement_session.session (also prints a session string — removal in 0.6)
│   └── clean_test_data.py        # ⚠ substring-matching HARD delete — see Tribal Knowledge #3
├── dev.db                        # Local SQLite database (git-ignored)
│
└── app/
    ├── __init__.py
    ├── config.py                 # Pydantic Settings — all env vars
    ├── database.py               # Async engine, session factory, get_db()
    ├── models.py                 # SQLAlchemy 2.0 ORM models
    ├── schemas.py                # Pydantic v2 API request/response schemas
    ├── crud.py                   # Async CRUD operations (select/update/returning)
    ├── main.py                   # FastAPI app (API only): schema check, CORS, routers
    ├── worker.py                 # Ingestion worker process: Telethon, dispatcher bridge, loops, heartbeat
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
    │   ├── client.py             # Telethon client factory (refuses to build one when Telegram is disabled)
    │   ├── pre_filter.py         # Regex noise gate (should_filter_noise)
    │   ├── debouncer.py          # Sliding-window ChannelDebouncer
    │   ├── backfill.py           # 24h backfill + catch-up fetcher
    │   ├── ledger.py             # message_ledger: outcomes, catch-up cursor, retry backoff
    │   └── listener.py           # TelegramIngestionService (routing, monitored set, catch-up, retries)
    │
    └── ai/
        ├── __init__.py
        ├── client.py             # AsyncOpenAI factory (cached singleton)
        ├── schemas.py            # LLM extraction Pydantic models (relaxed defaults)
        ├── prompts.py            # System prompt builder (UTC + context injection)
        ├── extractor.py          # LLM inference + fallback chain + timeout
        ├── rule_extractor.py     # Regex fallback (⚠ verified harmful — removal in 0.5)
        └── dispatcher.py         # State-machine CRUD executor
```

---

## 3. Tech Stack & Dependencies

| Package | Version | Role |
|---|---|---|
| `fastapi` | ≥ 0.111.0 | ASGI web framework |
| `uvicorn[standard]` | ≥ 0.30.0 | ASGI server + hot-reload |
| `sqlalchemy` | ≥ 2.0.30 | ORM, async engine (`select`/`update`/`returning`) |
| `alembic` | ≥ 1.20.1 | Schema migrations (since Phase 0.2) and the startup revision check |
| `asyncpg` | ≥ 0.29.0 | PostgreSQL async driver (prod) |
| `aiosqlite` | ≥ 0.20.0 | SQLite async driver (dev + tests) |
| `pydantic` | ≥ 2.7.0 | Schema validation, `model_validate_json` |
| `pydantic-settings` | ≥ 2.2.0 | `BaseSettings` with `.env` loading |
| `python-dotenv` | ≥ 1.0.1 | `.env` file support |
| `telethon` | ≥ 1.36.0 | Telegram MTProto client |
| `openai` | ≥ 1.30.0 | AsyncOpenAI client (OpenAI-compatible endpoint) |
| `python-dateutil`, `tzdata`, `httpx`, `greenlet` | see `requirements.txt` | Date parsing, tz data, ASGI test client, SQLAlchemy async |

> [!NOTE]
> These are lower bounds — **nothing is pinned**. Installed in the local venv on 2026-10-22: fastapi 0.141.1, starlette 1.7.0, sqlalchemy 2.1.1, pydantic 2.13.5, openai 3.20.0, telethon 1.45.0, uvicorn 0.54.0, asyncpg 0.31.0, aiosqlite 0.22.1. A hashed lock file (compiled for Python 3.12) is planned for Phase 1.

---

## 4. Configuration (`app/config.py`)

All settings are loaded from `.env` via `pydantic-settings`. Defaults allow the app to boot without any env vars set.

| Setting | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./dev.db` (since 0.3; the old default carried a placeholder credential) | Production must set it explicitly |
| `APP_ENV` | `development` | Environment label |
| `TELEGRAM_API_ID` | `0` | MTProto App ID (0 = listener disabled) |
| `TELEGRAM_API_HASH` | `""` | MTProto App Hash |
| `TELEGRAM_SESSION_NAME` | `placement_session` | File session used when `TELEGRAM_SESSION_STRING` is empty |
| `TELEGRAM_SESSION_STRING` | `""` | Telethon `StringSession` token; takes precedence over the file session |
| `TELEGRAM_PHONE` | `None` | Used for interactive first-auth only |
| `MAIN_FEED_CHANNEL_ID` / `TEST_FEED_CHANNEL_ID` | `None` | Root feeds (merged with `system_config` values at boot) |
| `DEBOUNCE_DELAY_SECONDS` | `90.0` | Sliding window before batch dispatch |
| `LLM_API_KEY` / `GROQ_API_KEY` | `""` | Bearer token for the LLM endpoint (`LLM_API_KEY` wins) |
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1` | Override for OpenRouter / local vLLM |
| `LLM_MODEL` | `openai/gpt-oss-20b` | Model identifier |
| `INGEST_ENVIRONMENTS` | `TEST` | Feeds the worker reads (`MAIN`, `TEST` or `MAIN,TEST`). TEST-only by default so a development machine never processes the real feed; production sets `MAIN,TEST` |
| `CONFIG_REFRESH_SECONDS` / `CATCHUP_INTERVAL_SECONDS` / `RETRY_POLL_SECONDS` / `HEARTBEAT_SECONDS` | `60` / `900` / `60` / `30` | Worker loop intervals |
| `CATCHUP_MAX_AGE_HOURS` | `72` | Oldest message catch-up replays for a chat with history (24 h for a chat without history or after an un-ignore) |
| `DEADMAN_URL` | `None` | Optional dead-man's-switch URL, pinged every 5 min while Telegram is connected |

> **Dev `.env`** uses `DATABASE_URL=sqlite+aiosqlite:///./dev.db` and `TELEGRAM_API_ID=0`, so the server starts fully without Telegram credentials.

---

## 5. ORM Models (`app/models.py`)

> The schema is owned by Alembic (`alembic/versions/0001_baseline_schema.py`, Phase 0.2); the app never creates or alters tables. Every datetime column uses the `UTCDateTime` type: values are converted to UTC before writing and always read back as aware UTC (SQLite stores the UTC wall-clock; PostgreSQL uses `TIMESTAMP WITH TIME ZONE`). A naive datetime is assumed to be UTC and raises `NaiveDatetimeWarning` — an error in every verify suite. Constraint and index names follow `NAMING_CONVENTION` in `app/database.py`.

### 5.1 `Company`
| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | Auto-increment |
| `name` | String(255) | Display name |
| `normalized_name` | String(255), indexed | Lowercase stripped; lookup key. **Unique per environment** — `UNIQUE (normalized_name, environment_type)` since 0.2, so MAIN and TEST may share a name. ⚠ Extraction-time name lookups still ignore the environment until 0.5 |
| `environment_type` | String(16) | `MAIN` \| `TEST` (default `MAIN`) |
| `main_feed_msg_id` | BigInteger | Single-ID column — **never written by any code path** (no thread stitching yet) |
| `invite_token` | String(128), indexed | Invite hash or public username the subchannel was joined from — added in 0.2, written from 0.4 |
| `subchannel_id` | BigInteger UNIQUE | Telegram channel ID for routing |
| `tracking_mode` | String(32) | `ELIGIBLE` \| `SILENT_TRACK` \| `IGNORED` — legacy company-level mode; can drift from role-level modes (see §0.1 invariant 2) |
| `is_active` | Boolean | Soft-delete flag |
| `is_custom` | Boolean | Manual entry vs Telegram-discovered |
| `created_at` | UTCDateTime | UTC default |

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
| `latest_note_kind` | String(32) | `CRITERIA_RELAXATION` \| `GENERAL` — added in 0.2, written from 0.5 (only relaxations may re-open a SILENT_TRACK role, invariant 6) |
| `created_at` | UTCDateTime | UTC default |
| `updated_at` | UTCDateTime | Auto-updated on write |

### 5.3 `TimelineEvent`
| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `company_id` | FK → companies | CASCADE DELETE |
| `role_track_id` | FK → role_tracks | SET NULL on delete |
| `event_type` | String(64) | `IRIS_DEADLINE` \| `FORM_SUBMISSION` \| `PPT` \| `OA_WINDOW` \| `INTERVIEW` \| `CUSTOM` |
| `title` | String(255) | Human-readable label |
| `action_link` | Text | Form URL, OA portal, etc. |
| `start_time` | UTCDateTime | Optional event start |
| `end_time` | UTCDateTime NOT NULL | Deadline — drives all state logic |
| `user_status` | String(32) | `PENDING` \| `COMPLETED` |
| `completed_at` | UTCDateTime | Timestamp of user completion |
| `is_custom` | Boolean | **Currently always `True`** — `crud.create_custom_event` hardcodes it, including for LLM-created events; not a reliable provenance flag |
| `is_deleted` | Boolean | Soft-delete flag (`default=False`) |
| `version` | Integer | Form replacement counter |
| `needs_review` / `review_reason` | Boolean / Text | Uncertain extractions are kept but flagged for the user (recall-first) — added in 0.2, written from 0.5 |
| `source_chat_id` / `source_message_id` | BigInteger | Telegram message that last created or updated the event (provenance, deep links); composite index `ix_timeline_events_source` — added in 0.2, written from 0.4/0.5 |
| `created_at` | UTCDateTime | |

**Computed Property — `display_state`:**
```
user_status=PENDING  + end_time > now  → "PENDING"          (Blue badge)
user_status=PENDING  + end_time < now  → "MISSED"           (Red badge)
user_status=COMPLETED + end_time > now → "COMPLETED_ACTIVE" (Green badge)
user_status=COMPLETED + end_time < now → "COMPLETED_EXPIRED"(Grey badge)
```
> ✅ Since 0.2, `display_state` compares against an aware UTC `end_time` (`UTCDateTime`), so an IST deadline flips to MISSED at the right instant. The old naive→UTC coercion, which mis-read IST-written rows by 5.5 h (gap H1), is gone.

### 5.4 `MessageLedger` (`message_ledger`, added in 0.2 — used from 0.3/0.4)
One row per processed Telegram message: idempotency, the catch-up cursor (max `message_id` per chat) and retry scheduling.

| Column | Type | Notes |
|---|---|---|
| `chat_id`, `message_id` | BigInteger, composite PK | One row per message |
| `status` | String(16) | `DONE` \| `FAILED` \| `SKIPPED` (check constraint) |
| `reason` | Text | Skip reason or last error |
| `attempts`, `next_attempt_at` | Integer, UTCDateTime | Retry scheduling; index `ix_message_ledger_retry (status, next_attempt_at)` |
| `processed_at` | UTCDateTime | Last update |

### 5.5 `WorkerStatus` (`worker_status`, added in 0.2 — used from 0.3)
Single-row liveness record (`CHECK (id = 1)`) written by the ingestion worker and read by the API's status endpoint: `telegram_state`, `last_update_at`, `last_llm_ok_at`, `last_error`, `catchup_at`, `heartbeat_at`.

> **⚠ Schema Gap vs MASTER_SPEC:** `event_type` currently uses Phase 2 naming (`OA_WINDOW`, `IRIS_DEADLINE`) while spec/LLM schema uses `event_category` with values `OA`, `IRIS`, `FORM`, `GD`, etc. Needs a unified enum before the extractor can write to DB.
> **⚠ Schema Gap:** `round_number` (INTEGER) is absent — required for state-aware OA/Interview round counting.
> **⚠ Schema Gap:** `has_attachments` (Boolean) is absent — required for paperclip indicator (PRD FR-2).

---

## 6. REST API Endpoints

### 6.1 Registered Routes

> ⚠ No route requires authentication (gap C7).

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
| `PATCH` | `/companies/{id}/tracking` | `ToggleTrackingPayload` | `CompanyResponse` (cascades `tracking_mode` to child `RoleTrack`s; does not set `is_triaged` — invariant 9, fix in 0.5) |
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
- `create_telegram_client()` → `TelegramClient` using `StringSession` when `TELEGRAM_SESSION_STRING` is set, **otherwise the file session** `placement_session.session` (present on disk, git-ignored; created by `scripts/login_telegram.py`)
- Built without `catch_up`: updates missed while disconnected are not replayed (gap C4)
- `flood_sleep_threshold=60` — auto-sleeps on `FloodWaitError` up to 60 s; longer waits raise, and a join that hits one is dropped
- **Guard (0.3):** raises `RuntimeError` instead of building a client while Telegram is disabled (`TELEGRAM_API_ID=0` or empty `TELEGRAM_API_HASH`) — constructing one would load the live session's auth key.

> **Activation gate:** Only the ingestion worker (`python -m app.worker`) creates a Telegram client, and it exits with code 0 when `TELEGRAM_API_ID=0`. The API process contains no Telegram code at all.

### 7.2 Pre-Filter (`pre_filter.py`)
`should_filter_noise(text, has_media) -> bool`

**Decision tree (in order):**
1. `has_media=True` → **KEEP** unconditionally (JD PDFs, schedule images)
2. `text` empty/whitespace → **DROP**
3. `len(text) < 8` → **DROP**
4. Matches `_ADMIN_CHATTER_RE` (SPOC routing, "dm me", "ping us") → **DROP**
5. No match in `_SIGNAL_RE` (dates, times, "deadline", "form", "oa", "interview", "iris", "ppt", "ctc", "shortlist", "http", …) → **DROP**
6. Otherwise → **KEEP**

> ⚠ Step 4 matches chatter words *anywhere* in the message and step 5 lacks common signals; verified to drop 8 of 9 realistic announcements (gap C2). Messages with link previews survive only because `has_media` also counts `MessageMediaWebPage`.

### 7.3 Debouncer (`debouncer.py`)
`ChannelDebouncer` — one instance per process, keyed by `chat_id`.

- **Sliding window:** Each `add_message()` cancels the current `asyncio.Task` and schedules a fresh one for `DEBOUNCE_DELAY_SECONDS` (default 90s)
- **On fire:** Buffer is popped, messages sorted by `date` ASC, `callback(chat_id, messages)` awaited
- **`flush_all()`:** Force-dispatches all pending buffers on graceful shutdown
- **Zero unhandled exceptions:** Callback errors are caught and logged; task cancellation uses explicit `try/except CancelledError`
- **`pending_ids(chat_id)` / `discard(chat_id)` (0.3):** expose buffered message ids (so catch-up never re-adds them) and drop a chat's buffer when it stops being monitored (e.g. IGNORED)
- ⚠ No maximum wait (a busy chat can postpone dispatch indefinitely); the slot is popped before the callback runs, so two dispatches for one chat can overlap (gap H3). Buffers are still in memory, but since 0.3 anything lost on a crash is recovered by catch-up.

### 7.4 Backfill (`backfill.py`)
`fetch_24h_backfill(client, entity) -> List[dict]`

- Cutoff = `now(UTC) - 24h`
- Fetches recent messages backwards from now with defensive safety cap (`limit=200`), filtering `msg_date >= cutoff` (resolves Telethon `offset_date` + `reverse=True` bug).
- Returns chronological payload matching live message contract: `[{index, label "[N]", id, text, raw_text, date, timestamp, has_media}, …]`
- ⚠ Triggered after every successful link, including re-posts of an already-linked invite; since 0.3 messages already in the ledger, buffered or in flight are skipped, so a re-post no longer re-processes the channel. All remaining messages (≤200) still go to the LLM in one call.
- `fetch_messages_since(client, entity, min_id, max_age, limit=200)` (0.3): raw messages newer than `min_id` and younger than `max_age`, oldest first — used by catch-up.

### 7.5 Listener (`listener.py`) & Two-Tier Ingestion
`TelegramIngestionService`:

- **Monitored set (0.3):** `refresh_monitored()` loads the root feeds (`.env` + `system_config`, filtered by `INGEST_ENVIRONMENTS`) and the subchannels of ingestible companies (`crud.get_ingestible_subchannel_ids`: active, ingested environment, not effectively IGNORED — invariants 2–3). The worker refreshes it every 60 s; chats that leave the set have their buffers discarded, chats that join it are reported for a bounded catch-up (invariant 4). The old per-message `_is_known_subchannel` database fallback is gone, so routing never queries the database.
- **Task tracking:** spawned `asyncio.Task` references are kept in `self._background_tasks` with error-logging callbacks.
- **Ledger (0.3):** every message gets an outcome — pre-filtered chatter and invite posts `SKIPPED` immediately, dispatched batches `DONE`, or `FAILED` when the payload handler raises (see §7.6).

| Tier | Feeds / Target | Routing Logic |
|---|---|---|
| **Tier 1 (Root Feeds)** | Configured `main_feed_id` or `test_feed_id` | 1. Reply-to stitching: parent context merged.<br>2. Invite link scan (`t.me/+...`, `t.me/joinchat/...`, `t.me/<username>`): auto-joins (`ImportChatInviteRequest` / `JoinChannelRequest`), links `Company` via `crud.link_or_create_subchannel_company` (`SILENT_TRACK`, `is_triaged=False`, stamped with parent `environment_type`), adds to `active_subchannel_ids`, and triggers 24h backfill.<br>3. Direct announcements (no invite): routed through `ChannelDebouncer` to Groq. |
| **Tier 2 (Sub-channels)** | Any `chat_id` in `active_subchannel_ids` (ingestible companies only) | 1. Noise gate (`pre_filter.should_filter_noise`).<br>2. Sliding debouncer (`ChannelDebouncer`, 90s).<br>3. Dispatches consolidated payload to Groq (`openai/gpt-oss-20b`) via `dispatcher.process_and_dispatch`. |

**Read-Only Safety & Feed Isolation Policy:**
- **Zero Outbound/Destructive Operations:** Strictly an incoming event listener (`events.NewMessage`, `events.MessageEdited`). No `send_message`, `delete_messages`, `edit_message`, or `LeaveChannelRequest`. Auto-joining subchannels via `ImportChatInviteRequest` / `JoinChannelRequest` is the only permitted Telegram action.
- **Feed Isolation:** Automatically drops private DMs (`event.is_private`) and every chat outside the monitored set — including channels of IGNORED or deleted companies (no pre-filter, no LLM call, no ledger row).
- **Join gate (0.3):** after linking a joined channel, the company must be ingestible; otherwise the channel is neither monitored nor backfilled. If the database link fails, the channel is not monitored.
- **Process:** runs only inside the ingestion worker (§7.7), never in the API.

> **⚠ Spec Gap:** Recursive reply fetching (depth ≤ 6) is not implemented — only single-level parent stitching exists. This affects deeply threaded announcements.
> **⚠ Spec Gap:** 60-second TCP heartbeat job (DC keep-alive) is not implemented.
> **⚠ Verified gaps:** when a root-feed post contains an invite link, its own text is never extracted (C3); public `t.me/<name>` links are resolved and joined without checking that the entity is a channel, and the 24 h backfill bypasses the `is_private` guard (C6). Edits are re-processed as new text with no message provenance (H3).

### 7.6 Ledger, Catch-up & Retries (`ledger.py`, listener) — Phase 0.3
- **Outcomes:** `message_ledger` holds one row per message: `DONE`, `SKIPPED` (with a reason: `pre_filter`, `invite_post`, `message_deleted`) or `FAILED`. Stored error text keeps only the exception class and first line — never SQL parameters or message text.
- **Retries:** a failed batch is retried after 1 m, 5 m, 30 m, then every 2 h; after 6 consecutive failures it stays `FAILED` and is logged as given up. The worker polls due retries every 60 s, re-fetches the messages by id (deleted ones become `SKIPPED message_deleted`) and re-dispatches them with the chat's original context. Chats that are no longer monitored are left alone.
- **Catch-up:** at boot and every 15 min, each monitored chat is re-scanned from 100 messages behind its ledger cursor (a crash can leave buffered messages below a later recorded one). The scan is bounded by `CATCHUP_MAX_AGE_HOURS` (72 h), or 24 h for a chat with no history or one that was just un-ignored or added (invariant 4). Messages already recorded, buffered or in flight are skipped; the rest are routed exactly like live messages.
- **Entity cache:** catch-up and retries resolve chats from the Telethon session's entity cache. A chat that isn't cached yet (e.g. a brand-new session) is skipped until its first live update caches it — so production should use the file session, which persists the cache (a `StringSession` loses it on restart).
- **Delivery guarantee:** at-least-once. If a ledger write fails, the message may be processed again but is never lost.
- ⚠ Until Phase 0.5, catch-up and backfill messages still have relative dates ("tomorrow") resolved against processing time (gap H2), and LLM failures don't reach the retry loop because the extractor swallows them (gap C4). Edits made while the worker was down aren't caught up — only new messages are.

### 7.7 Ingestion Worker (`app/worker.py`) — Phase 0.3
Run with `python -m app.worker`, **exactly one per Telegram session**.
1. Exits 0 immediately if Telegram is disabled (`TELEGRAM_API_ID=0` or no API hash) — no client is built and the database isn't touched.
2. Checks the database is at the Alembic head, connects, and verifies authorization. If unauthorized or the session is rejected, it records `UNAUTHORIZED` / `SESSION_INVALID`, waits `AUTH_FAILURE_WAIT_SECONDS` (10 min) and exits 2.
3. Loads the monitored set, registers handlers, and wires `dispatch_to_ai` (one DB session per batch; errors propagate so the batch is recorded `FAILED`).
4. Runs the listener plus four loops: boot catch-up then every 15 min; monitored-set refresh every 60 s (newly monitored chats get the 24 h catch-up); retries every 60 s; heartbeat every 30 s into `worker_status`. A failing loop iteration is logged and the loop continues.
5. Exits 1 if the listener stops or crashes (state `DISCONNECTED`; safe to restart) and 2 on session errors (`SESSION_INVALID`). On SIGTERM/SIGINT it records `STOPPED` and exits 0 after flushing buffers (30 s cap; anything unflushed is recovered by catch-up).
- **Heartbeat:** writes `telegram_state`, `heartbeat_at`, `last_update_at`, `last_llm_ok_at` (last successful dispatch), `catchup_at` and `last_error`. When `DEADMAN_URL` is set it pings it at most every 5 min, only while connected.

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

> ⚠ Per-message post timestamps are not injected, so relative dates resolve against processing time (gap H2). The schedule block appears only when the company is already resolved — never for main-feed posts — includes soft-deleted events, and carries no event IDs (gaps H4, H5). The user profile (`system_config`) is never injected (gap H6).

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

> ⚠ Because the extractor never raises, an LLM failure still ends as an empty (or regex) result that is recorded `DONE` — the retry loop added in 0.3 only sees errors that propagate. Making extraction failures raise is Phase 0.5 (gap C4). The regex tier is under revision (gap C5, Tribal Knowledge #2).

### 8.4 Dispatcher & Pre-Interview State Engine (`ai/dispatcher.py`)
`process_and_dispatch(chat_id, context_header, messages, db)`

**5-Step Pipeline** (verified defects: H3–H6, M1, M2 — see §0.2):
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

> ⚠ Verified behaviour (2026-10-21): `EXTEND_DEADLINE` updates only `end_time` (point deadlines keep the old `start_time`); `REPLACE_FORM` inserts a new version and leaves the previous form visible; `STATUS_NOTE` re-triages on *any* note, not only criteria relaxations (conflicts with §0.1 invariant 6); INDIVIDUAL-scope actions are dropped even when addressed to the user; a failed action is not rolled back, so later actions in the same batch can fail.

**Temporal Normalization Rules (`_normalize_event_times`):**
- *Live events* (`OA_WINDOW`, `PPT`, `INTERVIEW`): If only `start_time` is present, defaults `end_time = start_time + 2 hours`.
- *Deadlines* (`FORM_SUBMISSION`, `IRIS_DEADLINE`, `CUSTOM`): If only one timestamp is present, sets `start_time = end_time = timestamp`.
- *Start/End present:* Preserves exact bounds.

### 8.5 Dispatcher Bridge (`app/worker.py`)
```python
async def dispatch_to_ai(chat_id, context_header, messages):
    async with database.AsyncSessionLocal() as db:
        await dispatcher.process_and_dispatch(chat_id=chat_id, context_header=context_header, messages=messages, db=db)

await service.set_payload_handler(dispatch_to_ai)
```
Each batch opens an **isolated `AsyncSession`**. Since 0.3 errors **propagate**: the listener records the batch `FAILED` and the retry loop re-dispatches it. The old bridge in `main.py` logged and swallowed them.

---

## 9. Process Lifecycles (`app/main.py`, `app/worker.py`)

```
API — uvicorn app.main:app                    Worker — python -m app.worker (one per session)
  Startup                                       ├─ TELEGRAM_API_ID=0 → exit 0 (no client, no DB)
  └─ assert_schema_current()                    ├─ assert_schema_current()
  yield ←── FastAPI serves requests             ├─ connect + is_user_authorized → else exit 2 (after 10 min)
  Shutdown                                      ├─ refresh monitored set, register handlers, wire dispatch_to_ai
  └─ engine.dispose()                           ├─ listener + loops (catch-up, refresh, retries, heartbeat)
                                                ├─ listener ends → exit 1 · session error → exit 2
                                                └─ SIGTERM → flush (30 s cap), disconnect, exit 0
```

> ✅ Since Phase 0.2 the app never creates or alters tables: `alembic upgrade head` owns the schema, and both processes refuse to start when the database isn't at the head revision (the old `create_all` + suppressed `ALTER`s, gap H7, are gone). ✅ Since Phase 0.3 Telegram lives only in the worker (gap H8): the API can run several uvicorn workers without touching the session.

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

- ⚠ No authentication, and the origin regex reflects any origin with credentials (gap C7). React Native clients don't need CORS — removal planned in 0.6.
- Bound on `0.0.0.0:8000` locally (earlier sessions used 8080)
- Exposed publicly via `cloudflared tunnel --url http://localhost:8000` (quick tunnel: the URL changes on every restart)
- HTTPS termination at Cloudflare edge — satisfies Android API 28+ cleartext restrictions

---

## 11. Verification Scripts

All scripts are self-contained and require no live credentials or running server (except `test_live_ingestion.py` which requires read-only session credentials):

| Script | Coverage | DB |
|---|---|---|
| `verify_phase1.py` | ORM model instantiation, all 4 `display_state` branches, Pydantic schema round-trip | None |
| `verify_phase2.py` | CRUD mutations, drive-type viewport filtering, form versioning, deadline recovery, HTTP router tests (`POST /companies/` mobile contract, `GET /companies/{id}/events` 200/404, timeline `?env=MAIN\|TEST` isolation, complete/uncomplete) | In-memory SQLite |
| `verify_phase3.py` | Pre-filter DROP/KEEP cases, debounce sliding window, edit event propagation, 24h backfill, reply-to stitching, payload handler dispatch | Mock objects + in-memory SQLite (patched in 0.2) |
| `verify_phase4.py` | LLM schema validation, INDIVIDUAL guard, REPLACE_FORM, EXTEND_DEADLINE, prompt context injection, dirty subchannel payloads (missing company/drive), e2e CREATE_EVENT, e2e STATUS_NOTE, RESCHEDULE_EVENT start/end updates | In-memory SQLite |
| `verify_triage.py` | RoleTrack defaults, pending triage items, re-triage criteria relaxation, isolated fixtures, Company tracking mode cascade assertion, `/triage/pending`, `/accept`, `/ignore`, 404 endpoints | In-memory SQLite |
| `verify_settings.py` | SystemConfig ORM, GET/PUT `/settings` endpoints, partial update upsert | In-memory SQLite |
| `verify_schema.py` | Alembic upgrade → downgrade → upgrade + drift check, startup revision check, UTC round-trip, IST deadline flips to MISSED on time, naive-datetime policy, per-environment name uniqueness, foreign keys, ledger/status constraints | In-memory SQLite |
| `verify_worker.py` | `TELEGRAM_API_ID=0` builds no client; `app.main` imports no Telegram code; monitored set vs invariants 2–4; un-ignore/new-chat 24 h catch-up; ledger outcomes and backoff; retry loop; catch-up skipping; heartbeat + dead-man ping; exit codes; error propagation. The real `TelegramClient` class is replaced by one that fails if constructed | In-memory SQLite (migrated) |
| `scripts/test_live_ingestion.py` | Safe live preflight: Telethon auth session, Telegram entity resolution, Groq LLM check, read-only safety audit | Live API (Read-only) |

> ⚠ Every suite mocks the LLM, and none contains realistic multi-sentence announcements — which is how gaps C2 and C5 passed as green.
> Since 0.2, every suite enforces SQLite foreign keys (as PostgreSQL does) and turns naive datetimes into errors (`NaiveDatetimeWarning`).

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

### Remediation plan — Phase 0 "stop silent losses" (approved 2026-10-22, branch `feature/phase-0-stop-losses`)

| Item | Scope | Status |
|---|---|---|
| 0.1 | Docs truth pass; root spec edits approved and applied 2026-10-23 | ✅ Done — reviewed |
| 0.2 | Schema foundation: Alembic baseline, `UTCDateTime`, env-scoped company uniqueness, `message_ledger` / `worker_status` tables, SQLite pragmas | ✅ Done — reviewed 2026-10-23 (settings row restored from the backup) |
| 0.3 | API / worker process split; ledger outcomes; monitored-set refresh, catch-up, retry and heartbeat loops | ✅ Done — pending owner review |
| 0.4 | Ingestion: pre-filter, invite-post extraction, join policy, debouncer, IGNORED ingestion gate | Planned |
| 0.5 | Extraction & dispatch: post timestamps, event IDs, regex-tier removal, invariants 1–9 | Planned |
| 0.6 | API key, CORS removal, secret hygiene, `.dockerignore` | Planned |
| 0.7 | Frontend: API-key pairing, needs-review pill, Telegram deep links | Planned |

Phase 1 (Oracle VM foundation, then a 1–2 week shadow run on the real MAIN feed) follows Phase 0.

### P0 — Core Pre-Interview State Machine & Mobile Contract (delivered; verified defects listed in §0.2)

| # | Item | File(s) | Status / Detail |
|---|---|---|---|
| 1 | **Implement `/triage` router** | `app/routers/triage.py` | ✅ **Resolved** — `GET /triage/pending`, `POST /triage/{id}`, `/accept`, `/ignore`, and conditional re-triage. |
| — | **Atomic Company Creation** | `app/crud.py`, `app/schemas.py` | ✅ **Resolved** — `POST /companies/` creates default `RoleTrack` atomically with role metadata. |
| — | **Enriched Timeline & Events** | `app/schemas.py`, `app/routers/companies.py` | ✅ **Resolved** — `TimelineEventResponse` enriched with `company_name`, `drive_type`, `status`; `GET /companies/{id}/events` implemented. |
| — | **Pre-Interview State Engine** | `app/ai/dispatcher.py`, `app/crud.py` | ⚠ **Delivered with verified defects (H3, H4, M1, M2)** — Canonical time normalization (live events +2h, deadlines same timestamp), non-existent event fallbacks (`EXTEND_DEADLINE`/`RESCHEDULE_EVENT`/`REPLACE_FORM` → `CREATE_EVENT`), form replacement versioning with PENDING reset, soft-delete filtering, and criteria relaxation re-triage via `crud.flag_for_retriage()`. |
| — | **Zero-Dependency IST Timezone** | `app/ai/prompts.py`, `app/ai/schemas.py` | ✅ **Resolved** — Native UTC+05:30 `timezone` representation eliminating Windows `tzdata` crashes. |
| — | **Dual-Tier AI Extraction & Fallbacks**| `app/ai/extractor.py`, `app/ai/rule_extractor.py` | ⚠ **Partly** — `max_tokens=4096` and the in-flight JSON fallback stay; the regex tier was verified harmful (C5) and is removed in 0.5. |
| — | **Database Isolation & Sanitization** | `verify_*.py`, `scripts/clean_test_data.py` | ✅ **Resolved (Phase 0.2)** — every database-touching suite binds `:memory:` (`verify_phase3.py` patched). `dev.db` was never committed (git history verified 2026-10-21). |

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
| 10 | **Auto-join private channels** | `telegram/listener.py` | ✅ **Resolved** — Auto-joins via `ImportChatInviteRequest` (hashes) / `JoinChannelRequest` (usernames) and links SQLite `Company` record. ⚠ Join scope is unsafe (C6) — fix in 0.4. |
| 11 | **60s TCP heartbeat** | `app/main.py` | Background task pinging Telegram DC to reset half-open connections |
| 12 | **`system_config` table & profile API** | `models.py`, `app/routers/settings.py` | ✅ **Resolved** — `GET /settings` and `PUT /settings` implemented with student profile and feed IDs. |
| 13 | **FCM push notification worker** | `app/workers/notifier.py` | 60s polling: `PENDING` events ±2h or ±30min from `start_time` → FCM dispatch |
| 14 | **Defaulter list LLM filter** | `ai/prompts.py`, `ai/extractor.py` | Inject `system_configs.roll_number` into prompt; drop if user not in defaulter list |

### P3 — Deployment

| # | Item | File(s) | Detail |
|---|---|---|---|
| 15 | **`Dockerfile`** | `Dockerfile` | Multi-stage build; Uvicorn entry on port 8080 |
| 16 | **`docker-compose.yml`** | `docker-compose.yml` | Services: `api` + `postgres:16`; named volume for DB persistence |
| 17 | **Alembic migrations** | `alembic/` | ✅ **Resolved (Phase 0.2)** — baseline revision `0001`; the app only verifies the revision at startup |
| 18 | **Switch `DATABASE_URL` to PostgreSQL** | `.env` (prod) | `asyncpg` driver; `BIGINT[]` array types only available in PostgreSQL |

---

## 13. Git Log (Recent)

```
78a7f21 docs: finalize project state and tribal knowledge for handoff
3533cb9 chore: strengthen gitignore rules
09cbaa5 chore(git): scrub dev.db from index, update .gitignore, add .env.example and production README
fbcd333 fix(tz): use cross-platform UTC+05:30 IST definition and add tzdata dependency
e850a9c feat(pre-interview): implement master spec time normalization, ist anchoring, form replacement, and test feed isolation
e9a894e feat(ecosystem): fallback extend to create, normalize event times, add ist regex fallback, and wire iris deadline frontend tag
```

> Commits `09cbaa5` and `3533cb9` only added ignore rules — no secret, session or database file was ever committed (full history verified 2026-10-21). Phase 0 work happens on `feature/phase-0-stop-losses`.

---

## 14. Server Startup Command

```bash
# Activate venv
.\venv\Scripts\Activate.ps1

# Apply schema migrations (required: the server refuses to start otherwise)
python -m alembic upgrade head

# Start the API (contains no Telegram code)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Start the ingestion worker (separate terminal) — exactly ONE per Telegram session
python -m app.worker

# Expose via Cloudflare tunnel (separate terminal)
cloudflared tunnel --url http://localhost:8000
```

> Swagger UI available at `http://localhost:8000/docs` during development.

> [!WARNING]
> With `TELEGRAM_API_ID` set, **the worker** connects the **live personal session**, processes the feeds in `INGEST_ENVIRONMENTS` (default `TEST` only) and auto-joins invite links found there. Never run two workers on the same session (Telegram revokes it). To keep a worker from connecting, set `TELEGRAM_API_ID=0` for that process only (PowerShell: `$env:TELEGRAM_API_ID="0"`) — never by editing `.env`. The API never connects to Telegram.
