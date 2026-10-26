# PROJECT_STATE.md — Placement Engine Backend
**Last Updated:** 2026-10-26 | **Version:** 1.0.0 — **Status: pre-production** (verified gaps in §0.2; remediation "Phase 0 — stop silent losses" in progress) | **Branch:** `feature/phase-0-stop-losses`

> Single source of truth for the backend's operational status, architecture, and immediate backlog. Cross-referenced against `MASTER_SPEC.md` and `PRODUCT REQUIREMENTS DOCUMENT.md`.
> **The code is the source of truth.** Every status claim in this file was re-verified against the code on 2026-10-22; intended-but-unbuilt behaviour belongs in `MASTER_SPEC.md`, not here.

---

## 0. Tribal Knowledge & Critical Engineering Invariants

> [!IMPORTANT]
> The following rules were forged from real-world runtime edge cases during live campus testing. Every engineer and AI agent operating in this repository **must** adhere strictly to these invariants:

1. **Timezone Representation (Zero-Dependency Native UTC+05:30 IST)**:
   - **Rule:** Timezones MUST be defined using standard library `datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")`.
   - **Why:** On Windows machines, `zoneinfo.ZoneInfo("Asia/Kolkata")` crashes fatally with `ZoneInfoNotFoundError: 'No time zone found with key Asia/Kolkata'` if the Python environment lacks the `tzdata` package. Because Indian Standard Time does not observe Daylight Saving Time (strictly fixed at UTC+05:30 year-round), the native `datetime.timezone` constructor provides 100% deterministic, cross-platform stability with zero external dependency risks.
   - **Enforcement:** Enforced in `app/ai/prompts.py`, `app/ai/schemas.py` and `app/ai/dispatcher.py`. In addition, `tzdata>=2024.1` is pinned defensively in `requirements.txt`.
   - **⚠ Storage gap (verified 2026-10-21):** SQLite's `DateTime(timezone=True)` drops the offset *without converting*. LLM/app writes persist IST wall-clock, `rule_extractor` writes persist UTC, and `display_state` reads everything back as UTC — so IST-written deadlines turn MISSED 5.5 h late. **✅ Fixed in Phase 0.2:** every datetime column now uses the `UTCDateTime` type (stored as UTC, read back as aware UTC), and API inputs must carry an offset (`AwareDatetime`). Times shown to the LLM are converted back to IST.

2. **Groq AI Extraction: Token Headroom, Structured Output & No Guessing** (revised in Phase 0.5):
   - **Rule:** Every completion in `app/ai/extractor.py` sets `max_completion_tokens=8192` (never the default). gpt-oss-20b is a reasoning model: its reasoning tokens share that budget with the JSON answer, and a 1024 cap truncated multi-action answers mid-JSON (`EOF while parsing a string`). A truncated answer (`finish_reason="length"`) raises `ExtractionFailed`.
   - **Output modes, in order:**
     1. **Strict structured output** — `response_format` `json_schema` with `strict: true` and `RESULT_JSON_SCHEMA` (`app/ai/schemas.py`); Groq documents strict mode for `openai/gpt-oss-20b` (constrained decoding, every field required, objects closed). If Groq rejects it but JSON-object mode then works, strict mode is turned off for the rest of the process.
     2. **JSON-object mode** — `response_format={"type": "json_object"}`, validated with Pydantic.
     3. **No response format** — only after Groq's `json_validate_failed`; JSON is cut out of the text (code fences stripped).
   - **No regex fallback.** The old Tier 3 (`rule_extractor.py`) was verified to create wrong events ("Round 2 of the OA is tomorrow at 5pm" → 02:00; "OA on 25th October at 5 PM" → today 17:00; a defaulter-list message → a FORM deadline) and fired on valid empty answers. It was deleted in Phase 0.5. Hard failures raise `ExtractionFailed`: the batch is recorded `FAILED` in the message ledger and retried with backoff — never guessed.
   - Keep `RESULT_JSON_SCHEMA` in step with `ExtractedEvent` / `LLMExtractionResult`; `verify_extraction.py` fails if they drift.

3. **Test Feed Isolation & In-Memory SQLite Requirement (`:memory:`)**:
   - **Rule:** All test verification suites (`verify_phase1.py` through `verify_phase4.py`, `verify_triage.py`, `verify_settings.py`) MUST strictly bind to isolated in-memory databases (`sqlite+aiosqlite:///:memory:`) and override `app.database.engine`.
   - **Why:** Running tests against local disk files (`dev.db`) causes test fixtures to pollute the real `MAIN` feed environment with mock companies (`FallbackTestCorp`, `Acme Corp`), corrupting state for live Telegram listeners and mobile clients.
   - **Enforcement:** Since Phase 0.2, every database-touching suite (`verify_phase2/3/4.py`, `verify_triage.py`, `verify_settings.py`, `verify_schema.py`, and later `verify_worker.py`, `verify_ingestion.py`) binds `:memory:`; `verify_phase3.py` was patched (it used to read the configured database through `_is_known_subchannel`). All suites also enable SQLite foreign keys and turn `NaiveDatetimeWarning` into an error.
   - **`scripts/clean_test_data.py` (rewritten in 0.6):** it only ever touches TEST-environment companies and is a dry run unless `--apply` is given (`--name` narrows it). The old version hard-deleted MAIN companies whose names merely contained "test", "acme", …

---

## 0.1 Tracking-Status Invariants (binding — set by the owner, 2026-10-22)

> [!IMPORTANT]
> These rules override `MASTER_SPEC.md` §3 where they differ (the matching MASTER_SPEC edit was approved and applied on 2026-10-23). All nine are enforced since Phase 0.5: items 2–4 at the router and in the monitored set (0.3), item 3's dispatcher re-check and main-feed attribution (0.4), and items 1, 2, 5, 6, 8 and 9 in `crud.py` and the dispatcher (0.5). `verify_extraction.py` tests them through the API.

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
| C2 † | Pre-filter drops real announcements: "ok/okay/noted/thanks" *anywhere* → drop; plurals, "tomorrow/tonight", "GD", "venue" aren't signals. 8 of 9 realistic samples dropped. | `app/telegram/pre_filter.py:53-102` | ✅ 0.4 (only messages with no visible text are skipped) |
| C3 | A root-feed post containing an invite link is only joined — its own text (roles, CTC, deadlines) never reaches the LLM; the company gets the channel title, "Default Role", FTE. | `app/telegram/listener.py:285-294`, `app/crud.py:530-551` | ✅ 0.4 (the post is extracted with its company, named by the LLM, else the channel title) |
| C4 | Downtime or LLM failure = permanent loss: no catch-up on reconnect, in-memory debounce buffers, no retry queue. | `app/telegram/client.py:41-47`, `app/ai/extractor.py:152-159` | ✅ Downtime 0.3 (ledger catch-up + retries); offline edits and failed joins 0.4; LLM failures raise `ExtractionFailed` and are retried 0.5 |
| C5 † | Regex fallback invents wrong events and fires on valid empty LLM results (see Tribal Knowledge #2). | `app/ai/extractor.py:66-77`, `app/ai/rule_extractor.py` | ✅ 0.5 (deleted; failures are retried instead) |
| C6 | Auto-join resolves any `t.me/<name>`: a person/bot username creates a company plus a 24 h backfill of the private DM history (sent to the LLM); permalinks join foreign channels; the private-hash fallback re-resolves the hash as a username. | `app/telegram/listener.py:40-47,363-366,376,425-435` | ✅ 0.4 (`join_policy.py`: channels and supergroups only; root feeds must be broadcast channels) |
| C7 | No authentication on any route; CORS reflects any origin with credentials. | `app/main.py:145-151` | ✅ 0.6 (`X-API-Key` on every route but `/health`; CORS removed; docs off in production) |
| H1 † | Mixed IST/UTC wall-clock storage in SQLite (see Tribal Knowledge #1). | `app/models.py:145-158` | ✅ 0.2 |
| H2 | Message post times never reach the LLM; relative dates resolve against processing time (wrong for backfill, midnight, catch-up). | `app/ai/dispatcher.py:407-417`, `app/ai/prompts.py:90,130-137` | ✅ 0.5 (`[msg <id> · posted <IST> · edited]` headers; dates resolve against each message's post time) |
| H3 | Duplicates by construction: edits re-extracted as new text (no message provenance), invite re-posts re-run the 24 h backfill, contradictory prompt rules turn reminders into CREATE, no dedupe key, overlapping dispatches per chat. | `listener.py:415-421`, `prompts.py:94-96` vs `115-118`, `crud.py:292-318`, `debouncer.py:111-119` | ✅ 0.3–0.5 (ledger; edit collapse and per-chat order 0.4; event ids and `target_event_id`, edits update the events they created, same-time duplicates suppressed, reminder rule fixed 0.5) |
| H4 | Weak update targeting: match = latest event of the same type; EXTEND moves only `end_time`; REPLACE_FORM leaves the old form visible; deleted events are fed to the LLM and resurrect; no CANCELLED/TBA. | `dispatcher.py:57-62,108-128`, `crud.py:347-400` | ✅ 0.5 except CANCELLED/TBA (updates by id, ambiguous matches flagged, point deadlines move whole, superseded forms hidden, deleted events never shown to the LLM) |
| H5 | Entity/env isolation: exact-name match; name lookups ignore `environment_type`; `normalized_name` globally unique; `link_or_create_subchannel_company` overwrites `environment_type`; per-action names override the subchannel's company; main-feed calls get no schedule context. | `dispatcher.py:35-42,420-428,505-514`, `crud.py:511-525` | ✅ 0.2–0.5 (per-env uniqueness; env-scoped linking 0.4; source company authoritative, env-scoped name lookups, deleted names never re-created, mentioned companies' schedules for main-feed posts 0.5) |
| H6 | Profile unused: INDIVIDUAL messages dropped even when addressed to the user; defaulter, slot and shortlist logic unimplemented. | `dispatcher.py:497-503` | Next phase |
| H7 | On PostgreSQL the suppressed `ALTER`s abort the `create_all` transaction, whose COMMIT then rolls back (schema never created); `BOOLEAN DEFAULT 0` is invalid on PG. | `app/main.py:31-40` | ✅ 0.2 (Alembic; live-PG run is the first Phase 1 step) |
| H8 | Telethon runs inside the API process: >1 worker or two hosts on one session → `AUTH_KEY_DUPLICATED`; the listener task is unsupervised; `/health` always reports healthy. | `app/main.py:48-113,159-161` | ✅ 0.3 (separate worker process; liveness in `worker_status`; status endpoint 0.6) |
| H9 | The README Dockerfile copies `.env`, the session file and `dev.db` into the image (no `.dockerignore`); `login_telegram.py` prints the session string. | `README.md` (Docker), `scripts/login_telegram.py:66-69` | ✅ 0.6 (`.dockerignore`; no session string printed); hardened image Phase 1 |
| M1–M7 | No per-action rollback (✅ 0.5); every STATUS_NOTE re-triages (✅ 0.5, note kinds); the company toggle leaves `is_triaged=false` (✅ 0.5); the debouncer has no max-wait (✅ 0.4); Add Stage silently defaults to now+24 h; substring hard-delete script (✅ 0.6); documentation drift (this revision). | review 2026-10-21 | 0.2–0.7 |

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
├── alembic.ini, alembic/         # Schema migrations (env.py reads DATABASE_URL; versions/0001_baseline_schema.py, 0002_ledger_content_hash.py)
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
├── verify_ingestion.py           # Join policy, invite posts, edits/reactions, supergroups, debouncer suite (Phase 0.4)
├── verify_extraction.py          # Extractor, prompt, dispatcher and invariants 1–9 suite (Phase 0.5)
├── verify_security.py            # API key, /status, CORS/docs, startup policy, integrity errors, scripts, .dockerignore (Phase 0.6)
├── .dockerignore                 # Keeps .env*, sessions, databases, venv/ and .git/ out of images (0.6)
├── scripts/
│   ├── test_live_ingestion.py    # Live preflight against the real session (read-only; owner-run only)
│   ├── login_telegram.py         # Interactive login → placement_session.session (prints no session string since 0.6)
│   └── clean_test_data.py        # Removes TEST companies only; dry run unless --apply (0.6)
├── dev.db                        # Local SQLite database (git-ignored)
│
└── app/
    ├── __init__.py
    ├── config.py                 # Pydantic Settings — all env vars
    ├── database.py               # Async engine, session factory, get_db()
    ├── models.py                 # SQLAlchemy 2.0 ORM models
    ├── schemas.py                # Pydantic v2 API request/response schemas
    ├── crud.py                   # Async CRUD operations (select/update/returning)
    ├── main.py                   # FastAPI app factory (API only): key policy, schema check, routers, integrity errors
    ├── security.py               # X-API-Key dependency and the production key policy (0.6)
    ├── worker.py                 # Ingestion worker process: Telethon, dispatcher bridge, loops, heartbeat
    ├── sources.py                # MessageSource / SubchannelLink: where a batch came from (listener → dispatcher)
    │
    ├── routers/
    │   ├── __init__.py
    │   ├── companies.py          # /companies endpoints
    │   ├── events.py             # /events endpoints
    │   ├── triage.py             # /triage staging queue & action endpoints
    │   ├── settings.py           # /settings user profile & feed configuration
    │   └── status.py             # GET /status — ingestion worker heartbeat (0.6)
    │
    ├── telegram/
    │   ├── __init__.py
    │   ├── client.py             # Telethon client factory (refuses to build one when Telegram is disabled)
    │   ├── pre_filter.py         # Skips only messages with no visible text (skip_reason)
    │   ├── join_policy.py        # Which links are followed and which chats may be joined (pure)
    │   ├── debouncer.py          # Sliding-window ChannelDebouncer (max-wait, version collapse, per-chat order)
    │   ├── backfill.py           # Bounded fetch for catch-up and the 24 h backfill
    │   ├── ledger.py             # message_ledger: outcomes, catch-up cursor, retry backoff
    │   └── listener.py           # TelegramIngestionService (routing, monitored set, catch-up, retries)
    │
    └── ai/
        ├── __init__.py
        ├── client.py             # AsyncOpenAI factory (cached singleton)
        ├── schemas.py            # LLM extraction models + RESULT_JSON_SCHEMA (strict structured output)
        ├── prompts.py            # System prompt + [msg <id> · posted <IST> · edited] message headers
        ├── extractor.py          # LLM call (strict schema → JSON object → no format); raises ExtractionFailed
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
| `APP_ENV` | `development` | `development`, `testing` or `production`. Any other value counts as production (fail-closed): API docs off and `API_KEY` required |
| `API_KEY` | unset | Shared key every request sends as `X-API-Key` (0.6). Production refuses to start without one of ≥ 32 characters; unset in development leaves the API open (warning at startup). Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `TELEGRAM_API_ID` | `0` | MTProto App ID (0 = listener disabled) |
| `TELEGRAM_API_HASH` | `""` | MTProto App Hash |
| `TELEGRAM_SESSION_NAME` | `placement_session` | File session used when `TELEGRAM_SESSION_STRING` is empty |
| `TELEGRAM_SESSION_STRING` | `""` | Telethon `StringSession` token; takes precedence over the file session |
| `TELEGRAM_PHONE` | `None` | Used for interactive first-auth only |
| `MAIN_FEED_CHANNEL_ID` / `TEST_FEED_CHANNEL_ID` | `None` | Root feeds (merged with `system_config` values). Since 0.4 a feed must be a broadcast channel (`-100…` ID); anything else is refused and reported in `worker_status.last_error` |
| `DEBOUNCE_DELAY_SECONDS` | `90.0` | Sliding window before batch dispatch |
| `DEBOUNCE_MAX_WAIT_SECONDS` | `120.0` | A busy chat is flushed at most this long after its first buffered message (0.4) |
| `LLM_API_KEY` / `GROQ_API_KEY` | `""` | Bearer token for the LLM endpoint (`LLM_API_KEY` wins) |
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1` | Override for OpenRouter / local vLLM |
| `LLM_MODEL` | `openai/gpt-oss-20b` | Model identifier |
| `LLM_REASONING_EFFORT` | unset | `low` \| `medium` \| `high`; unset = the provider's default. Lower is cheaper and faster but may resolve dates less carefully — to be tuned with shadow-run data (0.5) |
| `INGEST_ENVIRONMENTS` | `TEST` | Feeds the worker reads (`MAIN`, `TEST` or `MAIN,TEST`). TEST-only by default so a development machine never processes the real feed; production sets `MAIN,TEST` |
| `CONFIG_REFRESH_SECONDS` / `CATCHUP_INTERVAL_SECONDS` / `RETRY_POLL_SECONDS` / `HEARTBEAT_SECONDS` | `60` / `900` / `60` / `30` | Worker loop intervals |
| `CATCHUP_MAX_AGE_HOURS` | `72` | Oldest message catch-up replays for a chat with history (24 h for a chat without history or after an un-ignore) |
| `DEADMAN_URL` | `None` | Optional dead-man's-switch URL, pinged every 5 min while Telegram is connected |

> **Dev `.env`** uses `DATABASE_URL=sqlite+aiosqlite:///./dev.db` and `TELEGRAM_API_ID=0`, so the server starts fully without Telegram credentials.

---

## 5. ORM Models (`app/models.py`)

> The schema is owned by Alembic (`alembic/versions/0001_baseline_schema.py`, Phase 0.2; `0002_ledger_content_hash.py`, Phase 0.4); the app never creates or alters tables. Every datetime column uses the `UTCDateTime` type: values are converted to UTC before writing and always read back as aware UTC (SQLite stores the UTC wall-clock; PostgreSQL uses `TIMESTAMP WITH TIME ZONE`). A naive datetime is assumed to be UTC and raises `NaiveDatetimeWarning` — an error in every verify suite. Constraint and index names follow `NAMING_CONVENTION` in `app/database.py`.

### 5.1 `Company`
| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | Auto-increment |
| `name` | String(255) | Display name |
| `normalized_name` | String(255), indexed | Lowercase stripped; lookup key. **Unique per environment** — `UNIQUE (normalized_name, environment_type)` since 0.2, so MAIN and TEST may share a name. ⚠ Extraction-time name lookups still ignore the environment until 0.5 |
| `environment_type` | String(16) | `MAIN` \| `TEST` (default `MAIN`) |
| `main_feed_msg_id` | BigInteger | Root-feed message id of the company's latest single-company invite post (written since 0.4): a reply to that post is attributed to the company. Single-ID column (see the gap below) |
| `invite_token` | String(128), indexed | Invite hash or public username the subchannel was joined from (written since 0.4): a re-post of a known token needs no Telegram request |
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
| `latest_note_kind` | String(32) | `CRITERIA_RELAXATION` \| `GENERAL` — written since 0.5 with every status note; only a relaxation re-opens a SILENT_TRACK role (invariant 6). Exposed in triage and role responses |
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
| `needs_review` / `review_reason` | Boolean / Text | Uncertain extractions are kept but flagged for the user (recall-first) — written since 0.5: low confidence, a date before its announcement or over a year away, an extension that moves earlier, an unknown target id, an ambiguous match, an edit whose earlier event can't be identified. Never cleared automatically — the user clears it with `POST /events/{id}/resolve-review` (0.7) |
| `source_chat_id` / `source_message_id` | BigInteger | Telegram message that last created or updated the event (provenance, deep links, edit matching); composite index `ix_timeline_events_source` — written since 0.5 (never guessed in a multi-message batch) |
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
| `content_hash` | String(64) | SHA-256 of the text that was handled (0.4, migration `0002`): an edit update whose text is unchanged is ignored, and catch-up replays messages edited while offline |

### 5.5 `WorkerStatus` (`worker_status`, added in 0.2 — used from 0.3)
Single-row liveness record (`CHECK (id = 1)`) written by the ingestion worker and read by the API's status endpoint: `telegram_state`, `last_update_at`, `last_llm_ok_at`, `last_error`, `catchup_at`, `heartbeat_at`.

> **⚠ Schema Gap vs MASTER_SPEC:** `event_type` currently uses Phase 2 naming (`OA_WINDOW`, `IRIS_DEADLINE`) while spec/LLM schema uses `event_category` with values `OA`, `IRIS`, `FORM`, `GD`, etc. Needs a unified enum before the extractor can write to DB.
> **⚠ Schema Gap:** `round_number` (INTEGER) is absent — required for state-aware OA/Interview round counting.
> **⚠ Schema Gap:** `has_attachments` (Boolean) is absent — required for paperclip indicator (PRD FR-2).

---

## 6. REST API Endpoints

### 6.1 Registered Routes

> **Authentication (0.6):** every route except `/health` requires the `X-API-Key` header, compared in constant time with `API_KEY` (`app/security.py`); a missing or wrong key → **401**. A production server without a key would answer **503** — it refuses to start anyway. Missing records referenced by a request return **404** (path) or **422** (body), duplicates **409**, other constraint violations **422** — never a 500 or SQL text.

#### Meta
| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Public liveness probe of the API process; returns only `{"status": "ok"}` |
| `GET` | `/status` | Authenticated. The ingestion worker's heartbeat from `worker_status`: `status` (`ok` = fresh heartbeat and Telegram connected, `degraded`, `unknown` = never ran), `telegram_state`, `heartbeat_at`, `heartbeat_age_seconds`, `last_update_at`, `last_llm_ok_at`, `catchup_at`, `last_error` |

#### Companies — `/companies`
| Method | Path | Request Body / Params | Response |
|---|---|---|---|
| `POST` | `/companies/` | `CompanyCreate` (accepts `name`, `company_name`, `target_role`, `role_name`, `drive_type`, `ctc_stipend_info`, `environment_type="MAIN"`) | `CompanyResponse` (201) — atomically creates parent company and default `RoleTrack` (`is_triaged=True`, `tracking_mode="ELIGIBLE"`); **409** if the name exists in that environment |
| `GET` | `/companies/` | `?tracking_mode=&env=MAIN\|TEST` | `List[CompanyResponse]` (filters `is_active == True`, serializes both `name` and `company_name`) |
| `GET` | `/companies/{id}/events` | — | `List[TimelineEventResponse]` (chronological, filters `is_deleted == False`, returns 404 if company does not exist) |
| `PATCH` | `/companies/{id}/tracking` | `ToggleTrackingPayload` | `CompanyResponse` (cascades `tracking_mode` and `is_triaged=true` to every child `RoleTrack` — invariant 9). The only path out of IGNORED (invariant 1) |
| `POST` | `/companies/{id}/tracks` | `RoleTrackCreate` | `RoleTrackResponse` (201); **404** for an unknown company; **409** for an effectively IGNORED company (a new role would bring it back) |
| `DELETE` | `/companies/{id}` | — | `{"status": "deleted", "id": id}` (soft-deletes company `is_active=False` & cascades `is_deleted=True` to events) |

#### Timeline Events — `/events`
| Method | Path | Request Body / Params | Response |
|---|---|---|---|
| `GET` | `/events/timeline` | `?drive_type=FTE&env=MAIN\|TEST` | `List[TimelineEventResponse]` (LEFT OUTER JOIN on `RoleTrack` so company-level events are preserved; filters `Company.is_active == True`, company not IGNORED, role ELIGIBLE and `TimelineEvent.is_deleted == False`; serializes `display_state`/`status`, and since 0.5 `needs_review`, `review_reason`, `source_chat_id`, `source_message_id`) |
| `POST` | `/events/` | `TimelineEventCreate` | `TimelineEventResponse` (201); **422** if `company_id` does not exist or `role_track_id` is not one of its roles |
| `PATCH` | `/events/{id}/complete` | `?completed=true` | `TimelineEventResponse` |
| `POST` | `/events/{id}/uncomplete` | — (alias `/reopen`) | `TimelineEventResponse` (resets `user_status=PENDING`, `completed_at=None`) |
| `PATCH` | `/events/{id}/extend` | `ExtendDeadlinePayload` | `TimelineEventResponse` |
| `POST` | `/events/companies/{id}/replace-form` | `ReplaceFormPayload`, `?role_track_id=` | `TimelineEventResponse` (201); hides the superseded form (0.5); **404** for an unknown company, **422** for a role of another company |
| `POST` | `/events/{id}/resolve-review` | — | `TimelineEventResponse`; clears `needs_review` and `review_reason` after the user checked the event (0.7); **404** for an unknown event. A later uncertain update can flag it again |
| `DELETE` | `/events/{id}` | — | `{"status": "deleted", "id": id}` (soft-deletes event `is_deleted=True`) |

#### Triage Hub — `/triage`
| Method | Path | Request Body / Params | Response |
|---|---|---|---|
| `GET` | `/triage/pending` | `?env=MAIN\|TEST` | `List[TriageItemResponse]` (`is_triaged == False`, never IGNORED roles or companies, newest first; includes `environment_type`, `latest_status_note`, `latest_note_kind`) |
| `GET` | `/triage` | `?env=MAIN\|TEST` | Alias to `/triage/pending` |
| `POST` | `/triage/{id}` | `TriageActionPayload` | `TriageItemResponse` (sets `tracking_mode` & `is_triaged=True`, cascades `ELIGIBLE` to the parent `Company`; discarding the last tracked role marks the company IGNORED). **409** for an IGNORED role or a role of an IGNORED company (invariants 2, 5) |
| `POST` | `/triage/{id}/accept` | — | `TriageItemResponse` (shortcut for `ELIGIBLE`; same 409 rule) |
| `POST` | `/triage/{id}/ignore` | — | `TriageItemResponse` (shortcut for `IGNORED`; same 409 rule) |

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
- `flood_sleep_threshold=60` — auto-sleeps on `FloodWaitError` up to 60 s; longer waits raise. Since 0.4 a join that hits one leaves its invite post `FAILED`, retried after the wait (§7.5)
- **Guard (0.3):** raises `RuntimeError` instead of building a client while Telegram is disabled (`TELEGRAM_API_ID=0` or empty `TELEGRAM_API_HASH`) — constructing one would load the live session's auth key.

> **Activation gate:** Only the ingestion worker (`python -m app.worker`) creates a Telegram client, and it exits with code 0 when `TELEGRAM_API_ID=0`. The API process contains no Telegram code at all.

### 7.2 Pre-Filter (`pre_filter.py`) — rewritten in 0.4
`skip_reason(text) -> Optional[str]`

A message is skipped (ledger `SKIPPED`, reason `empty`) **only** when it has no visible text: `None`, empty, whitespace, or only invisible characters (zero-width spaces/joiners, BOM, Hangul fillers). Everything else — "Slot 1", "Room 204", "Link active", "ok" — goes on to the debouncer and the LLM, in both tiers. Owner decision 2026-10-23: placement channels are announcement-only, so the old length, chatter and keyword rules lost more announcements than they saved tokens (gap C2). Only text and media captions are extracted, so media without a caption counts as empty.

### 7.3 Debouncer (`debouncer.py`)
`ChannelDebouncer` — one instance per process, keyed by `chat_id`.

- **Sliding window:** each `add_message()` restarts the countdown (`DEBOUNCE_DELAY_SECONDS`, default 90 s).
- **Max wait (0.4):** a buffer is flushed at most `DEBOUNCE_MAX_WAIT_SECONDS` (120 s) after its first message, however busy the chat.
- **Version collapse (0.4):** a newer version of a buffered message (an edit) replaces it, so each message appears once, in its latest form. `is_edit` stays false when the original was never dispatched.
- **Per-chat order (0.4):** batches of one chat never overlap — a flush waits for the previous one (one `asyncio.Lock` per chat). Ids waiting for or inside the callback still count in `pending_ids()`, so catch-up never re-adds them.
- **On fire:** the buffer is popped, sorted by `date` ASC, and `callback(chat_id, messages)` awaited; callback errors are logged, never raised into the event loop.
- **`flush_all()`** on graceful shutdown; **`discard(chat_id)`** when a chat stops being monitored (e.g. IGNORED).

### 7.4 Backfill (`backfill.py`)
`fetch_messages_since(client, entity, min_id=0, max_age=24h, limit=200)` → raw messages newer than `min_id` and younger than `max_age`, oldest first (Telethon iterates newest-first, so the scan stops at the cutoff; a warning is logged if the cap is hit). Used by catch-up. Since 0.4 the 24 h backfill of a newly linked channel is a catch-up with a 24 h window: its messages are routed like live ones (same filters, debouncer and ledger). The old one-call `fetch_24h_backfill` is gone.

### 7.5 Listener (`listener.py`) & Two-Tier Ingestion
`TelegramIngestionService`:

- **Monitored set (0.3):** `refresh_monitored()` loads the root feeds (`.env` + `system_config`, filtered by `INGEST_ENVIRONMENTS`) and the subchannels of ingestible companies (`crud.get_ingestible_subchannel_ids`: active, ingested environment, not effectively IGNORED — invariants 2–3). The worker refreshes it every 60 s; chats that leave the set have their buffers discarded, chats that join it get a bounded catch-up (invariant 4). Routing never queries the database for membership.
- **Subscriptions:** only `events.NewMessage` and `events.MessageEdited`. Reaction updates (`UpdateMessageReactions`, `UpdateBotMessageReaction(s)`) are never subscribed to — Telethon builds neither event from them — and `message.reactions` is never read: the payload carries only the message text or caption (owner decision 2026-10-23).
- **Edit updates (0.4):** an edit is processed only if its text changed. An update without `edit_date`, one whose text hashes the same as the version already handled (`message_ledger.content_hash`), or an edit more than 24 h old of a message never processed here is ignored, since Telegram may re-send a message when only metadata such as reactions changed (not verified; the check makes it harmless either way).
- **Batches (0.4):** at most 40 messages per LLM call; a buffer is split so each batch belongs to one company. The dispatcher puts each message under a `[msg <id> · posted <IST> · edited]` header (0.5).
- **Task tracking:** spawned `asyncio.Task` references are kept in `self._background_tasks` with error-logging callbacks.
- **Ledger:** every message gets an outcome (§7.6).

| Tier | Feeds / Target | Routing Logic |
|---|---|---|
| **Tier 1 (Root Feeds)** | `main_feed_id` / `test_feed_id` — must be **broadcast channels** (0.4) | 1. No visible text → `SKIPPED empty`.<br>2. **Invite post** (links per §7.8), handled in a background task. Each link is looked up — a known `invite_token` needs no Telegram request — or previewed and joined under the join policy, at most one link per 30 s. *One channel:* the post is extracted with that company. A new channel is linked by the dispatcher under the extracted company name (else the channel title), then monitored with a 24 h backfill. An IGNORED or deleted company → `SKIPPED company_ignored`, with no LLM call. *Several channels:* each is linked under its title and the post is extracted as a plain announcement. *Every link refused:* plain announcement. A flood wait or network error leaves the post `FAILED`; it is retried, never before the wait ends, and other joins pause meanwhile.<br>3. **Other posts:** a reply to a company's single-company invite post is attributed to that company (or `SKIPPED company_ignored`); the parent's text is stitched as `[CONTEXT: …]`; debounced, then extracted. |
| **Tier 2 (Sub-channels)** | `active_subchannel_ids` (ingestible companies only) | 1. No visible text → `SKIPPED empty`.<br>2. In a **supergroup**, only admins' posts (including anonymous admins) count; others are `SKIPPED non_admin_sender`. The admin list is read once per 24 h; if it can't be read, messages are kept (recall-first).<br>3. Debounced, then extracted via `dispatcher.process_and_dispatch`. |

**Read-Only Safety & Feed Isolation Policy:**
- **Zero outbound/destructive operations:** no `send_message`, `delete_messages`, `edit_message` or `LeaveChannelRequest`. The only state-changing Telegram actions are joins (`ImportChatInviteRequest` / `JoinChannelRequest`) that the join policy allows. Invite previews (`CheckChatInviteRequest`), username resolution (`ResolveUsernameRequest`) and supergroup admin lists (`channels.getParticipants`) are read-only. Links are followed only from root-feed posts, never from subchannels, and `get_dialogs()` is never called.
- **Feed isolation:** private chats and every chat outside the monitored set are dropped — including IGNORED or deleted companies' channels (no LLM call, no ledger row). A root feed that is not a broadcast channel (a group, person or bot — e.g. a mistyped ID) is never read or joined from; the problem is logged once and surfaced in `worker_status.last_error`.
- **Environments:** attribution never crosses environments — a TEST-feed post is never attached to a MAIN company's channel, and vice versa.
- **Process:** runs only inside the ingestion worker (§7.7), never in the API.

> **⚠ Spec Gap:** Recursive reply fetching (depth ≤ 6) is not implemented — only single-level parent stitching exists.
> **⚠ Spec Gap:** 60-second TCP heartbeat job (DC keep-alive) is not implemented.
> ✅ Since 0.5 messages are individually addressed in the prompt (ids, post times, edited flag), events record their source message, and an edit updates the event it created (§8.4).
> ⚠ In a discussion group linked to a broadcast channel, auto-forwarded channel posts arrive "from" that channel and are `SKIPPED non_admin_sender`. Company channels are expected to be broadcast channels.
> ⚠ A fresh session bootstraps a root feed from its last 24 h only, so invite posts older than that are never seen. A wider one-time bootstrap for the VM's first start is a Phase 1 decision.

### 7.6 Ledger, Catch-up & Retries (`ledger.py`, listener) — Phase 0.3, extended in 0.4
- **Outcomes:** `message_ledger` holds one row per message: `DONE`, `FAILED`, or `SKIPPED` with a reason — `empty`, `company_ignored`, `non_admin_sender`, `message_deleted` (the 0.3 reasons `pre_filter` and `invite_post` are gone). Each row keeps the hash of the text it handled (0.4). Stored error text keeps only the exception class and first line — never SQL parameters or message text.
- **Retries:** a failed message is retried after 1 m, 5 m, 30 m, then every 2 h — or after a Telegram flood wait, if that is longer; after 6 consecutive failures it stays `FAILED` and is logged as given up. The worker polls due retries every 60 s, re-fetches the messages by id (deleted ones become `SKIPPED message_deleted`) and since 0.4 routes them again like live messages, so a retried invite post re-runs its joins and every filter applies. Chats that are no longer monitored are left alone.
- **Catch-up:** at boot and every 15 min, each monitored chat is re-scanned from 100 messages behind its ledger cursor (a crash can leave buffered messages below a later recorded one). The scan is bounded by `CATCHUP_MAX_AGE_HOURS` (72 h), or 24 h for a chat with no history or one that was just un-ignored, added or linked (invariant 4). Messages already recorded, buffered or in flight are skipped; the rest are routed exactly like live messages. Since 0.4, recorded messages whose text has changed since (edited while offline) are routed again as edits. Edits older than the scan (100 messages behind the cursor, 72 h) are not replayed.
- **Entity cache:** catch-up and retries resolve chats from the Telethon session's entity cache. A chat that isn't cached yet (e.g. a brand-new session) is skipped until its first live update caches it — so production should use the file session, which persists the cache (a `StringSession` loses it on restart).
- **Delivery guarantee:** at-least-once. If a ledger write fails, the message may be processed again but is never lost.
- ✅ Since 0.5, relative dates resolve against each message's post time (so catch-up and backfill batches are dated correctly), and LLM failures raise `ExtractionFailed`, so they reach the retry loop (gaps H2, C4).

### 7.7 Ingestion Worker (`app/worker.py`) — Phase 0.3
Run with `python -m app.worker`, **exactly one per Telegram session**.
1. Exits 0 immediately if Telegram is disabled (`TELEGRAM_API_ID=0` or no API hash) — no client is built and the database isn't touched.
2. Checks the database is at the Alembic head, connects, and verifies authorization. If unauthorized or the session is rejected, it records `UNAUTHORIZED` / `SESSION_INVALID`, waits `AUTH_FAILURE_WAIT_SECONDS` (10 min) and exits 2.
3. Loads the monitored set, registers handlers, and wires `dispatch_to_ai` (one DB session per batch; errors propagate so the batch is recorded `FAILED`).
4. Runs the listener plus four loops: boot catch-up then every 15 min; monitored-set refresh every 60 s (newly monitored chats get the 24 h catch-up); retries every 60 s; heartbeat every 30 s into `worker_status`. A failing loop iteration is logged and the loop continues.
5. Exits 1 if the listener stops or crashes (state `DISCONNECTED`; safe to restart) and 2 on session errors (`SESSION_INVALID`). On SIGTERM/SIGINT it records `STOPPED` and exits 0 after flushing buffers (30 s cap; anything unflushed is recovered by catch-up).
- **Heartbeat:** writes `telegram_state`, `heartbeat_at`, `last_update_at`, `last_llm_ok_at` (last successful dispatch), `catchup_at` and `last_error`. When `DEADMAN_URL` is set it pings it at most every 5 min, only while connected.

### 7.8 Join Policy (`join_policy.py`) — Phase 0.4
Pure functions (no client, no I/O), unit-tested in `verify_ingestion.py`:
- `find_invite_links(text)` — the followable links in a root-feed post. Private invites: `t.me/+<hash>`, `t.me/joinchat/<hash>`, `tg://join?invite=`. Public channels: `t.me/<username>`, `t.me/s/<username>`, `<username>.t.me`, `tg://resolve?domain=`. `telegram.me` and `telegram.dog` are aliases. Never followed: message permalinks (`t.me/<name>/<id>`, `t.me/c/…`), phone links (`t.me/+<digits>`), links with a query (bot `?start=`, Mini Apps, livestreams), and folder, sticker, theme, proxy, login and share links.
- `check_invite(preview)` / `check_chat(chat)` — only channels and supergroups qualify. Refused: persons and bots (`person_or_bot`), basic groups (`not_a_channel`), the root feeds (`root_feed`), and invites that need admin approval (`needs_approval` — joining would send the admins a join request).
- `is_channel_id(id)` (root feeds must have `-100…` IDs) and `is_broadcast_channel(chat)` (root feeds must be broadcast channels, where only admins post).
- The listener previews a private invite with `messages.checkChatInvite` before importing it, and resolves a username with `contacts.resolveUsername` directly. It never uses Telethon's string heuristics, which treat a phone number as a contact lookup. The old fallback that re-resolved a private hash as a username is gone (gap C6). Invite hashes are logged masked (`t.me/+AbCd…`).

---

## 8. AI Extraction & Dispatch Pipeline (`app/ai/`)

### 8.1 Extraction Schemas (`ai/schemas.py`)

| Type | Values |
|---|---|
| `ActionType` | `CREATE_EVENT`, `EXTEND_DEADLINE`, `RESCHEDULE_EVENT`, `REPLACE_FORM`, `STATUS_NOTE`, `NO_ACTION` |
| `DriveType` | `FTE`, `INTERNSHIP`, `COMBINED` (default `"FTE"`) |
| `AudienceScope` | `COHORT`, `INDIVIDUAL` (default `"COHORT"`) |
| `EventType` | `IRIS_DEADLINE`, `FORM_SUBMISSION`, `PPT`, `OA_WINDOW`, `INTERVIEW`, `CUSTOM` |
| `Confidence` (0.5) | `HIGH`, `MEDIUM`, `LOW` — LOW results are kept but flagged `needs_review` |
| `NoteKind` (0.5) | `CRITERIA_RELAXATION`, `GENERAL` — a missing kind counts as GENERAL |

`ExtractedEvent` fields: `action`, `company_name`, `role_name`, `drive_type`, `audience_scope`, `event_type`, `title` (falls back to `event_type`), `action_link`, `start_time`, `end_time` (naive values are read as IST), `status_note`, and since 0.5 `note_kind`, `target_event_id` (id of the scheduled event an action updates), `source_message_id` (from the message header) and `confidence`. Unknown `note_kind`/`confidence` labels become null instead of failing the batch.

`RESULT_JSON_SCHEMA` is the same contract as a strict JSON schema for Groq structured outputs (every property required, nullable fields as unions with null, objects closed).

> **⚠ Schema Divergence vs MASTER_SPEC §4:** The current `ExtractedEvent` is a simplified Phase 4 schema. The spec's canonical `PlacementExtractionPayload` requires: `is_actionable`, `roles[]` (with `lifecycle_note`), `events[].event_category` (RESUME/IRIS/FORM/OA/GD/…), `events[].round_number`, `events[].is_form_replacement`, `events[].venue`.

### 8.2 Prompt Builder (`ai/prompts.py`)
`build_system_prompt(context)` and `format_message_block(messages)`, rebuilt for every call (0.5):
- **Message headers:** each message in the user turn starts with `[msg <id> · posted <weekday, date, HH:MM> IST · edited]`. A `[CONTEXT: …]` line quotes the parent of a reply.
- **Dates:** relative dates resolve against the **posted time in the header** of the message that contains them, not the current time; a date without a year is the next such date on or after the post (gap H2).
- **Known Companies & Their Current Schedule:** for each company, its roles and its non-deleted events (not those of IGNORED roles) as `id=<n> — [TYPE] — title — role — starts/ends (IST) — link — status — from msg <id>`. The source company is marked "every message below is about this company"; a root-feed post gets the tracked companies it mentions by name.
- **Rules:** one event, one entry — create only what is not scheduled; a reminder of a scheduled event is NO_ACTION; a later deadline is EXTEND_DEADLINE, a moved slot RESCHEDULE_EVENT, a superseding form REPLACE_FORM, each with `target_event_id`; an edited message updates the entries "from msg <id>" it created. Also `source_message_id`, per-action `company_name` for digest posts, audience scope, note kinds (CRITERIA_RELAXATION only when eligibility was widened; never judge the student's eligibility) and confidence levels. The old contradictory rules that turned reminders into new events are gone (gap H3).
- **Output format:** JSON formatting rules and the schema example (mirrors `RESULT_JSON_SCHEMA`).

> ⚠ The user profile (`system_config`) is never injected (gap H6); CGPA and branch are never collected (invariant 7).

### 8.3 Extractor (`ai/extractor.py`)
`extract_placement_actions(raw_text, current_context) -> LLMExtractionResult` — raises `ExtractionFailed`.

- Cached `AsyncOpenAI` client (`get_ai_client()`) with `max_retries=3` for transient errors (429, 5xx, timeouts); 60 s per request; `temperature=0.0`; `max_completion_tokens=8192`; `reasoning_effort` only when `LLM_REASONING_EFFORT` is set.
- **Output modes:** strict `json_schema` → JSON-object mode (strict mode switched off for the process if Groq rejects it but JSON-object mode works) → no format after `json_validate_failed` (Tribal Knowledge #2).
- **Valid results are returned** — including one with no actions. **Everything else raises `ExtractionFailed`:** request errors left after the client's retries, a rejected request, an empty or truncated answer, output that doesn't match the schema. The message ledger records the batch `FAILED` (with a short reason, never message text) and retries it with backoff (§7.6). There is no regex fallback (gap C5, deleted in 0.5).

### 8.4 Dispatcher & Pre-Interview State Engine (`ai/dispatcher.py`)
`process_and_dispatch(chat_id, context_header, messages, db, company_id=None, subchannel=None)` — rewritten in 0.5.

1. **Format** the messages under their headers.
2. **Source company** — authoritative for every action (per-action company names are not followed): `company_id` (a reply to, or re-post of, its invite post), the channel an invite post just joined (`subchannel`), or the subchannel itself. A subchannel without a company, or an IGNORED/deleted source company, stops before the LLM call.
3. **Context:** the source company's schedule with event ids, or for a root-feed post the tracked companies it mentions.
4. **Extraction** (`ExtractionFailed` propagates → retried). An invite post's channel is then linked with `crud.link_or_create_subchannel_company` under the extracted company name, else the channel title (only an active same-environment company without a channel is reused; it stays IGNORED if it was).
5. **Actions:** INDIVIDUAL scope and NO_ACTION are skipped. Root-feed actions go to the company their name matches **within the feed's environment**; an unknown name becomes a SILENT_TRACK company with an untriaged default role (Triage); a deleted or IGNORED company's actions are dropped and never re-created. An action for an IGNORED role is dropped. Each action runs on its own: a failure is rolled back and the rest of the batch continues (M1).

| Action | Behaviour |
|---|---|
| `CREATE_EVENT` | Creates the event (times normalized below) — unless `target_event_id` names an existing event, or the message is an edit of one that created an event of that type, in which case that event is updated. An event of the same company and type within 1 minute of the same time is not created twice. |
| `EXTEND_DEADLINE` | Moves the target's deadline; a point deadline (start = end) moves whole. Target: `target_event_id`, else the event the edited message created, else the company's event of that type (and named role) — the latest one, flagged, if several. No match → created. |
| `RESCHEDULE_EVENT` | Sets the target's new start and end (and link, if given); same target rules. |
| `REPLACE_FORM` | With a link: inserts version `max + 1` (PENDING) and soft-deletes the form it supersedes (H4). Without a link: reschedules the form. No form found → created as `FORM_SUBMISSION`. |
| `STATUS_NOTE` | Stores the note and its kind on the named role, else on every role that is not IGNORED. Only `CRITERIA_RELAXATION` on a SILENT_TRACK role re-opens it in Triage; `tracking_mode` never changes; IGNORED roles are never touched (invariant 6). |
| `NO_ACTION` | No-op. |

**Provenance and review:** every created or updated event records its source message (`source_chat_id`, `source_message_id`) — the action's `source_message_id` if it is in the batch, else the only message of a one-message batch, never a guess. Events are flagged `needs_review` (with the reasons) for low confidence, a date before its announcement or more than a year away, an extension that moves the deadline earlier, an unknown `target_event_id`, an ambiguous match, or an edit whose earlier event can't be identified.

> ⚠ Still open: INDIVIDUAL-scope actions are dropped even when addressed to the user (gap H6); there is no CANCELLED/TBA state (H4).

**Temporal Normalization Rules (`_normalize_event_times`):**
- *Live events* (`OA_WINDOW`, `PPT`, `INTERVIEW`): If only `start_time` is present, defaults `end_time = start_time + 2 hours`.
- *Deadlines* (`FORM_SUBMISSION`, `IRIS_DEADLINE`, `CUSTOM`): If only one timestamp is present, sets `start_time = end_time = timestamp`.
- *Start/End present:* Preserves exact bounds.

### 8.5 Dispatcher Bridge (`app/worker.py`)
```python
async def dispatch_to_ai(source: MessageSource, messages):
    async with database.AsyncSessionLocal() as db:
        await dispatcher.process_and_dispatch(
            chat_id=source.chat_id, context_header=source.context, messages=messages, db=db,
            company_id=source.company_id, subchannel=source.subchannel,
        )

await service.set_payload_handler(dispatch_to_ai)
```
`MessageSource` and `SubchannelLink` live in `app/sources.py` (0.4). Each batch opens an **isolated `AsyncSession`**. Since 0.3 errors **propagate**: the listener records the batch `FAILED` and the retry loop re-dispatches it. The old bridge in `main.py` logged and swallowed them.

---

## 9. Process Lifecycles (`app/main.py`, `app/worker.py`)

```
API — uvicorn app.main:app                    Worker — python -m app.worker (one per session)
  Startup                                       ├─ TELEGRAM_API_ID=0 → exit 0 (no client, no DB)
  ├─ check_api_key_configuration()              ├─ assert_schema_current()
  │   (production: no key ≥ 32 chars → refuse)  │
  └─ assert_schema_current()                    │
  yield ←── FastAPI serves requests             ├─ connect + is_user_authorized → else exit 2 (after 10 min)
  Shutdown                                      ├─ refresh monitored set, register handlers, wire dispatch_to_ai
  └─ engine.dispose()                           ├─ listener + loops (catch-up, refresh, retries, heartbeat)
                                                ├─ listener ends → exit 1 · session error → exit 2
                                                └─ SIGTERM → flush (30 s cap), disconnect, exit 0
```

> ✅ Since Phase 0.2 the app never creates or alters tables: `alembic upgrade head` owns the schema, and both processes refuse to start when the database isn't at the head revision (the old `create_all` + suppressed `ALTER`s, gap H7, are gone). ✅ Since Phase 0.3 Telegram lives only in the worker (gap H8): the API can run several uvicorn workers without touching the session.

---

## 10. Networking & API Security (Phase 0.6)

- **No CORS middleware.** The React Native app makes native requests, which CORS doesn't govern; browsers get no `Access-Control-Allow-Origin` header, so no web page can call the API with a visitor's browser.
- **Shared API key:** every route except `/health` requires `X-API-Key` (§6.1). The app pairs with the server URL and key in Phase 0.7; until then the current APK works only against a server without `API_KEY` (local development).
- **Docs:** `/docs`, `/redoc` and `/openapi.json` are served in development only.
- Bound on `0.0.0.0:8000` locally (earlier sessions used 8080).
- Exposed via Cloudflare. Locally a quick tunnel (`cloudflared tunnel --url http://localhost:8000`, URL changes on every restart); production uses a named tunnel on an owned domain (Phase 1). HTTPS terminates at the Cloudflare edge, which satisfies Android API 28+ cleartext restrictions.

---

## 11. Verification Scripts

All scripts are self-contained and require no live credentials or running server (except `test_live_ingestion.py` which requires read-only session credentials):

| Script | Coverage | DB |
|---|---|---|
| `verify_phase1.py` | ORM model instantiation, all 4 `display_state` branches, Pydantic schema round-trip | None |
| `verify_phase2.py` | CRUD mutations, drive-type viewport filtering, form versioning, deadline recovery, HTTP router tests (`POST /companies/` mobile contract, `GET /companies/{id}/events` 200/404, timeline `?env=MAIN\|TEST` isolation, complete/uncomplete) | In-memory SQLite |
| `verify_phase3.py` | Pre-filter (only blank messages skipped), debounce sliding window, version collapse of edits, bounded fetch window, reply-to stitching, payload handler dispatch | Mock objects + in-memory SQLite (patched in 0.2) |
| `verify_phase4.py` | LLM schema validation, INDIVIDUAL guard, REPLACE_FORM, EXTEND_DEADLINE, prompt context injection, dirty subchannel payloads (missing company/drive), e2e CREATE_EVENT, e2e STATUS_NOTE, RESCHEDULE_EVENT start/end updates | In-memory SQLite |
| `verify_triage.py` | RoleTrack defaults, pending triage items, note kinds (only CRITERIA_RELAXATION re-opens SILENT_TRACK; IGNORED roles untouched), isolated fixtures, Company tracking mode cascade assertion, `/triage/pending`, `/accept`, `/ignore`, 404 endpoints | In-memory SQLite |
| `verify_settings.py` | SystemConfig ORM, GET/PUT `/settings` endpoints, partial update upsert | In-memory SQLite |
| `verify_schema.py` | Alembic upgrade → downgrade → upgrade + drift check, startup revision check, UTC round-trip, IST deadline flips to MISSED on time, naive-datetime policy, per-environment name uniqueness, foreign keys, ledger/status constraints | In-memory SQLite |
| `verify_worker.py` | `TELEGRAM_API_ID=0` builds no client; `app.main` imports no Telegram code; monitored set vs invariants 2–4; un-ignore/new-chat 24 h catch-up; ledger outcomes and backoff; retry loop; catch-up skipping; heartbeat + dead-man ping; exit codes; error propagation. The real `TelegramClient` class is replaced by one that fails if constructed | In-memory SQLite (migrated) |
| `verify_ingestion.py` | Join-policy link and chat tables; short messages reach the LLM; reactions never subscribed to or read, unchanged-text edit updates ignored; invite posts joined, linked and extracted with their company, re-posts and replies attributed, IGNORED companies skipped; refused links (bot, person, basic group, approval, expired, phone, permalink, root feed) never joined; flood-wait retry and join pacing; supergroup admin filter; root feeds must be broadcast channels; debouncer max-wait and per-chat order; ≤ 40 messages per LLM call; offline edits caught up; dispatcher source company, naming and IGNORED gate; no cross-environment attribution. Every chat, person and bot is a mocked entity | In-memory SQLite (migrated) |
| `verify_extraction.py` | Strict JSON-schema requests (schema closed and in step with the model); `ExtractionFailed` for invalid/empty/truncated answers, timeouts, network and rate limits (no regex fallback); output-mode fallbacks; failed extraction → ledger FAILED → retried to DONE; message headers and prompt rules; updates by `target_event_id`; edits update instead of duplicating; same-time duplicates suppressed; `needs_review` flags; source message ids; superseded forms hidden; note kinds; env-scoped root-feed names; per-action rollback; invariants 1–9 through the API (409s, last-discard sync, timeline filters, PATCH triages every role, no profile data to the LLM); `resolve-review` clears the flag (0.7) | In-memory SQLite (migrated) |
| `verify_security.py` | Every operation but `/health` requires `X-API-Key` (OpenAPI schema and live calls: missing, wrong, truncated → 401); constant-time compare; `/health` minimal, `/status` ok/degraded/unknown; no CORS headers; docs off in production; APP_ENV fail-closed; production startup refused without a strong key; 404/409/422 instead of 500; login script prints no session string (static check — never run); `clean_test_data.py` dry-run and TEST-only; `.dockerignore` coverage | In-memory SQLite (migrated) |
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
python verify_schema.py
python verify_worker.py
python verify_ingestion.py
python verify_extraction.py
python verify_security.py
# Owner-run only (uses the live session, read-only):
python scripts/test_live_ingestion.py
```

---

## 12. Immediate Backlog (Spec Conformance Gaps)

### Remediation plan — Phase 0 "stop silent losses" (approved 2026-10-22, branch `feature/phase-0-stop-losses`)

| Item | Scope | Status |
|---|---|---|
| 0.1 | Docs truth pass; root spec edits approved and applied 2026-10-23 | ✅ Done — reviewed |
| 0.2 | Schema foundation: Alembic baseline, `UTCDateTime`, env-scoped company uniqueness, `message_ledger` / `worker_status` tables, SQLite pragmas | ✅ Done — reviewed 2026-10-23 (settings row restored from the backup) |
| 0.3 | API / worker process split; ledger outcomes; monitored-set refresh, catch-up, retry and heartbeat loops | ✅ Done — reviewed 2026-10-23 |
| 0.4 | Ingestion: empty-only pre-filter, join policy, invite-post extraction and attribution, reactions ignored and edit detection, supergroup admin filter, debouncer max-wait and order, ≤ 40 messages per LLM call | ✅ Done — reviewed 2026-10-26 |
| 0.5 | Extraction & dispatch: message headers and post-time dates, event ids and `target_event_id`, regex fallback removed and `ExtractionFailed` retried, strict structured output, note kinds, review flags and source ids, invariants 1–9 | ✅ Done — reviewed 2026-10-26 |
| 0.6 | API security: `X-API-Key` on every route but `/health`, authenticated `/status`, CORS removed, docs off in production, production refuses to start without a key, 404/409/422 instead of 500, safe login and cleanup scripts, `.dockerignore` | ✅ Done — reviewed 2026-10-26 |
| 0.7 | Frontend: secure pairing (URL + key), 401 → re-pair, status dot from `/status`, needs-review pill + `POST /events/{id}/resolve-review` (backend), Telegram deep links, https-only links, Add Stage validation, note-kind Triage banner | ✅ Done — pending owner review; new APK build is the owner's step |

Phase 1 (Oracle VM foundation, then a 1–2 week shadow run on the real MAIN feed) follows Phase 0.

### P0 — Core Pre-Interview State Machine & Mobile Contract (delivered; verified defects listed in §0.2)

| # | Item | File(s) | Status / Detail |
|---|---|---|---|
| 1 | **Implement `/triage` router** | `app/routers/triage.py` | ✅ **Resolved** — `GET /triage/pending`, `POST /triage/{id}`, `/accept`, `/ignore`, and conditional re-triage. |
| — | **Atomic Company Creation** | `app/crud.py`, `app/schemas.py` | ✅ **Resolved** — `POST /companies/` creates default `RoleTrack` atomically with role metadata. |
| — | **Enriched Timeline & Events** | `app/schemas.py`, `app/routers/companies.py` | ✅ **Resolved** — `TimelineEventResponse` enriched with `company_name`, `drive_type`, `status`; `GET /companies/{id}/events` implemented. |
| — | **Pre-Interview State Engine** | `app/ai/dispatcher.py`, `app/crud.py` | ✅ **Reworked in 0.5** — updates by event id, edits update the events they created, duplicates suppressed, superseded forms hidden, review flags and source ids, note kinds, per-action rollback (gaps H3, H4, M1, M2). Open: INDIVIDUAL messages addressed to the user (H6), CANCELLED/TBA. |
| — | **Zero-Dependency IST Timezone** | `app/ai/prompts.py`, `app/ai/schemas.py` | ✅ **Resolved** — Native UTC+05:30 `timezone` representation eliminating Windows `tzdata` crashes. |
| — | **AI Extraction & Fallbacks** | `app/ai/extractor.py` | ✅ **Reworked in 0.5** — strict structured output with JSON-object and no-format fallbacks; the regex tier was deleted (C5) and failures are retried via the ledger. |
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
| 10 | **Auto-join private channels** | `telegram/listener.py`, `telegram/join_policy.py` | ✅ **Resolved** — invites previewed and joined via `ImportChatInviteRequest` / `JoinChannelRequest` under the join policy (0.4, gap C6); the post is extracted with its company. |
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

> Commits `09cbaa5` and `3533cb9` only added ignore rules — no secret, session or database file was ever committed (full history verified 2026-10-21). Phase 0 work happens on `feature/phase-0-stop-losses`: `a0b9b3b` (0.1), `3b1ccbe` (0.2), `dfee428` (0.3), `f3a241e` + `7d13962` (0.4), `76a0fd8` (0.5), `6bf21d1` (0.6), then 0.7.

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

> Swagger UI available at `http://localhost:8000/docs` in development only (use **Authorize** with the API key when `API_KEY` is set).

> [!WARNING]
> With `TELEGRAM_API_ID` set, **the worker** connects the **live personal session**, processes the feeds in `INGEST_ENVIRONMENTS` (default `TEST` only) and auto-joins the channel invite links posted there, under the join policy (§7.8). Never run two workers on the same session (Telegram revokes it). To keep a worker from connecting, set `TELEGRAM_API_ID=0` for that process only (PowerShell: `$env:TELEGRAM_API_ID="0"`) — never by editing `.env`. The API never connects to Telegram.
