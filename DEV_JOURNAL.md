# placement-engine/DEV_JOURNAL.md

[September 2026]: Telethon Message Fragmentation & LLM Context Loss
The Problem: Inbound Telegram messages in subchannels were often split across multiple fragmented posts, causing the Groq LLM to process incomplete context and fail entity extraction.
The Context/Why it failed: Telethon triggers `events.NewMessage` instantly for every individual chat bubble. Processing these sequentially led to redundant API calls and missing relational data.
The Decision/Fix: Implemented an in-memory sliding buffer with a 90 to 120-second debounce window. Messages are now held and merged into a single cohesive payload before triggering the Groq extraction logic.

[September 2026]: Thread Disconnection in Main Feed
The Problem: Replies containing schedule tables or updates posted in the main placement feed lost their association with the original company announcement.
The Context/Why it failed: Standard chronological parsing couldn't mathematically link detached follow-up schedules to their parent roles without explicit context.
The Decision/Fix: Leveraged Telegram's `reply_to_msg_id` header in the Telethon listener to stitch replies directly to the parent announcement. Rule established: Always map timeline events to the correct company using relational thread IDs.

[September 2026]: High Groq API Token Consumption & Noise
The Problem: The Groq LLM was frequently triggered by administrative noise, SPOC greetings, and non-temporal chatter, wasting tokens and risking hallucinations.
The Context/Why it failed: Routing every raw broadcast directly to the LLM without pre-filtering caused unnecessary API overhead and degraded the extraction quality.
The Decision/Fix: Built a deterministic regex pre-filter that automatically drops non-temporal messages and administrative noise before they reach the LLM pipeline. Rule established: The LLM is strictly reserved for high-signal payloads.

[September 2026]: Missed In-Place Schedule Modifications
The Problem: Deadlines and venues modified by placement coordinators hours after the initial announcement were not updating in the PostgreSQL database.
The Context/Why it failed: The Telethon listener was exclusively bound to `events.NewMessage`, completely ignoring subsequent message modifications in the Telegram channels.
The Decision/Fix: Registered the `events.MessageEdited` handler in Telethon. The system now tracks entities by unique `message_id` to catch and parse in-place modifications silently without duplicating database records.

[September 2026]: Missing Triage Endpoint (Frontend 404)
The Problem: The mobile application threw a `Not Found` error when attempting to load the Triage Hub staging queue.
The Context/Why it failed: The frontend API contract expected a `/triage` endpoint to fetch the manual-review queue, but `main.py` lacked this router in FastAPI.
The Decision/Fix: A temporary fallback was added to the frontend to catch the 404. Rule established: The backend must formally implement `@app.get("/triage")` and `@app.post("/triage/{id}/accept")` in FastAPI to resolve the API mismatch.

[October 2026]: Triage Hub Implementation & Conditional Criteria Relaxation
The Problem: Inbound role announcements needed a dedicated staging and manual review workflow before promotion to the active timeline feed, plus an automated mechanism to resurface drives when criteria change.
The Context/Why it failed: The application previously stored tracking state only at the company level. When placement teams relaxed CGPA or branch criteria, previously discarded/ignored roles remained hidden indefinitely without alerting the student.
The Decision/Fix: Implemented `app/routers/triage.py` exposing `GET /triage/pending`, `POST /triage/{id}`, and shortcut routes `/accept` and `/ignore`. Built `crud.flag_for_retriage()`, which updates `latest_status_note` on role tracks and conditionally resets `is_triaged = False` strictly when `tracking_mode == "SILENT_TRACK"`, ensuring criteria relaxation triggers a re-review banner in the mobile hub without disrupting accepted or ignored tracks.

[October 2026]: Role-Level Tracking Schema Migration
The Problem: Multi-role companies (e.g. hiring both SDE Full-Time and Analyst Intern) could not be tracked independently with different eligibility statuses.
The Context/Why it failed: The legacy schema bound `tracking_mode` directly to `Company`. Rejecting or accepting one role inadvertently altered all other roles from the same company.
The Decision/Fix: Migrated `tracking_mode`, `is_triaged`, `drive_type`, `ctc_stipend_info`, `lifecycle_status`, and `latest_status_note` to the `RoleTrack` model. Updated timeline query filters in `crud.get_timeline_events()` to join `RoleTrack` and strictly enforce `RoleTrack.tracking_mode == "ELIGIBLE"` with support for `COMBINED` viewport roles.

[October 2026]: System Configuration & Settings API
The Problem: Student identity metadata (name, roll number, college email) and Telegram channel feed IDs were unconfigurable at runtime, preventing customized LLM defaulter filtering and multi-environment feed subscription.
The Context/Why it failed: Hardcoding settings in `.env` prevented the mobile client from persisting user profile data dynamically.
The Decision/Fix: Introduced the `SystemConfig` ORM model (`system_config` table) and implemented `app/routers/settings.py` providing `GET /settings` and `PUT /settings` with `exclude_unset=True` partial updates. Verified behavior across uninitialized states and validated feed IDs as BigInteger storage.

[October 2026]: P0 Contract Alignment — Atomic Company Creation, Timeline Enrichment & Company Events Route
The Problem: Mobile client encountered HTTP 422 Unprocessable Entity when creating companies (`company_name` vs `name` and missing default `RoleTrack`), stage progression StepperView threw HTTP 404 on `GET /companies/{id}/events`, and timeline cards could not render status badges or company tags due to schema field discrepancies.
The Context/Why it failed: The frontend sent flat company objects containing role metadata (`role_name`, `drive_type`, `ctc_stipend_info`), while `POST /companies/` only accepted `CompanyBase` without role tracks. Furthermore, `TimelineEventResponse` omitted joined metadata (`company_name`, `drive_type`, `status`).
The Decision/Fix: 
1. Updated `CompanyCreate` schema to accept optional `role_name`, `drive_type`, and `ctc_stipend_info`. Updated `crud.create_company()` to atomically persist a child `RoleTrack` (`is_triaged=True` by default) in the same transaction.
2. Implemented `GET /companies/{company_id}/events` in `app/routers/companies.py` returning chronological events scoped to a specific company.
3. Added async-safe properties (`company_name`, `drive_type`, `status`) to `TimelineEvent` and enriched `TimelineEventResponse` schema with fallback defaults, eliminating runtime null dereferences on the frontend.

[October 2026]: Tracking Mode Cascade, Event Reopen Endpoint & Environment Type Isolation (`MAIN` vs `TEST`)
The Problem:
1. Updating a company's `tracking_mode` via `PATCH /companies/{id}/tracking` risked `MissingGreenlet` during Pydantic response serialization if `role_tracks` was not eagerly loaded, and did not cascade `tracking_mode` to child `RoleTrack` rows (leaving events visible in `GET /events/timeline` when a company was moved to `SILENT_TRACK` or `IGNORED`).
2. Completed events could not be reopened via a dedicated uncomplete endpoint (`POST /events/{id}/uncomplete`).
3. `Company` lacked an explicit `environment_type` (`MAIN` vs `TEST`) column and query filter across `/companies/`, `/events/timeline`, and `/triage/pending` to isolate Main Feed vs Test Feed data.
The Decision/Fix:
1. Updated `crud.update_company_tracking` to cascade `tracking_mode` updates to all child `RoleTrack` records and reload `Company` with `.options(selectinload(models.Company.role_tracks))`.
2. Added `crud.reopen_timeline_event` and `POST /events/{event_id}/uncomplete` (with `/reopen` alias) to reset `user_status = 'PENDING'` and `completed_at = None`.
3. Added `environment_type: Mapped[str] = mapped_column(String(16), default="MAIN", nullable=False)` to `models.Company` (with a startup SQLite `ALTER TABLE` guard in `app/main.py`), exposed `environment_type` on `CompanyCreate`, `CompanyResponse`, `TimelineEventResponse`, and `TriageItemResponse`, and added optional `?env=MAIN|TEST` filtering to `GET /companies/`, `GET /events/timeline`, and `GET /triage/pending`.

[October 2026]: Soft-Deletion Architecture for Companies & Timeline Events
The Problem: Users needed the ability to delete unwanted or erroneously created companies and timeline events/stages from the mobile client without breaking database foreign keys or allowing the Telegram ingestion pipeline to re-create deleted records on message edits/backfills.
The Context/Why it failed: Hard `DELETE` statements destroy relational history and `subchannel_id`/`main_feed_msg_id` mappings, which would cause Telethon backfill or edit handlers to re-ingest previously deleted companies or stages. Additionally, `TimelineEvent` lacked a dedicated soft-delete boolean column.
The Decision/Fix:
1. Added `is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)` to `models.TimelineEvent` with a safe `ALTER TABLE timeline_events ADD COLUMN is_deleted BOOLEAN DEFAULT 0` migration check in `app/main.py` (`lifespan`).
2. Implemented `crud.soft_delete_company(db, company_id)` (sets `Company.is_active = False` and cascades `TimelineEvent.is_deleted = True` across all associated events) and `crud.soft_delete_timeline_event(db, event_id)` (sets `TimelineEvent.is_deleted = True`).
3. Updated `crud.get_companies`, `crud.get_timeline_events`, `crud.get_company_events`, and `crud.get_pending_triage_items` to strictly filter `Company.is_active == True` and `TimelineEvent.is_deleted == False`.
4. Exposed `DELETE /companies/{company_id}` in `app/routers/companies.py` and `DELETE /events/{event_id}` in `app/routers/events.py`, returning `{"status": "deleted", "id": ...}`.

[October 2026]: Telegram Read-Only Safety Policy, Feed Isolation & Live Smoke Test
The Problem: Telegram ingestion needed strict runtime safety guarantees: ensuring zero risk of outbound messages, channel deletions, or leaving channels, and enforcing feed isolation so private chats, group DMs, and unconfigured channels are never processed.
The Context/Why it failed: `_process_event` previously routed any non-main-feed event directly to `_handle_subchannel`, which could inadvertently process unrelated chats if the authenticated user account was part of personal conversations.
The Decision/Fix:
1. Conducted codebase safety audit confirming zero occurrences of outbound message sending (`send_message`, `SendMediaRequest`), message deletions/edits (`delete_messages`, `edit_message`), or channel-leaving actions (`LeaveChannelRequest`, `DeleteChatRequest`). Confirmed company status updates perform purely local SQLite/PostgreSQL operations.
2. Implemented strict feed isolation in `TelegramIngestionService._process_event`: private chats (`event.is_private`) and messages from any `chat_id` not matching configured `main_feed_id` or `test_feed_id` (from Settings or `SystemConfig`) are dropped immediately at the event level.
3. Built `scripts/test_live_ingestion.py` for safe, non-interactive live preflight verification of Telegram auth (`client.get_me()`), Groq LLM connectivity (`openai/gpt-oss-20b`), target test channel resolution, and programmatic safety audit.

[October 2026]: Two-Tier Telegram Ingestion Workflow & Non-Interactive Lifespan Integration
The Problem: Inbound announcements from root feeds (Main & Test) and subchannels needed full alignment with `MASTER_SPEC.md` Section 3 and `PRD.md` Section 3: subchannels discovered via invite links (`t.me/+...`, `t.me/joinchat/...`, `t.me/<username>`) in root feeds must be auto-joined (`ImportChatInviteRequest` / `JoinChannelRequest`) and linked to SQLite Company records (`is_triaged = False`), while direct announcements route to the debouncer and Groq, and subchannels process updates with noise filtering and debounce before LLM extraction. Furthermore, FastAPI lifespan must boot Telethon non-interactively without freezing on stdin prompts.
The Context/Why it failed: Lifespan previously used `await tg_client.start(phone=...)`, which blocks on terminal input if a session is invalid or requires 2FA. Subchannels were not dynamically loaded into `active_subchannel_ids` on startup, and auto-joined subchannels lacked an atomic database helper stamping `environment_type` and `SILENT_TRACK`.
The Decision/Fix:
1. Updated `app/main.py` lifespan: connects non-interactively via `await tg_client.connect()` and checks `await tg_client.is_user_authorized()`. If authorized, calls `await tg_service.initialize()`, registers handlers, wires the payload handler, queries `await tg_service.get_root_feed_ids()`, logs `[TELEGRAM] Ingestion service active. Root feeds: Main=<id>, Test=<id>`, and spawns `asyncio.create_task(tg_client.run_until_disconnected())`. On shutdown, gracefully shuts down the debouncer and client.
2. Updated `app/telegram/listener.py`:
   - Tier 1: Monitors root feeds (`main_feed_id`, `test_feed_id`). Extracts invite links via regex. If an invite link exists, auto-joins via Telethon (`ImportChatInviteRequest` for private hashes, `JoinChannelRequest` for usernames), resolves signed peer ID via `utils.get_peer_id(entity)`, atomically creates/links a `Company` record in SQLite (`crud.link_or_create_subchannel_company`), adds the subchannel ID to `active_subchannel_ids`, and triggers 24-hour historical backfill. If no invite link is present, routes through debouncer for direct announcement parsing.
   - Tier 2: Monitors all chat IDs in `active_subchannel_ids`. Pre-filters non-temporal chatter, runs through `ChannelDebouncer`, and dispatches consolidated payloads to Groq (`openai/gpt-oss-20b`).
   - Private chat rejection: Immediately drops private messages (`event.is_private`) and unmonitored channels.
3. Enhanced `scripts/test_live_ingestion.py` to verify non-interactive Telegram login, target test channel entity resolution from `SystemConfig` (`Placement test sandbox`), and Groq completion response. All 6 verification suites passed cleanly.

[October 2026]: Phase 1 — Database Session Safety, CORS Regex, Backfill Semantics & AI Client Lifecycle
The Problem:
1. `get_db()` yielded sessions without an explicit rollback block on unhandled exceptions, risking dirty connections returning to the pool.
2. Wildcard CORS `allow_origins=["*"]` with `allow_credentials=True` violated W3C fetch standards, causing mobile/browser clients to reject preflight requests.
3. Telegram 24h backfill used inverted `offset_date` with `reverse=True`, dropping messages or breaking order, and its output dict format did not strictly conform to the live message contract.
4. Telethon listener invoked un-cached `await self.refresh_root_feeds()` per message event and lacked tracking of spawned background tasks.
5. Strict `ExtractedEvent` schema caused validation errors on null `drive_type`, `audience_scope`, `company_name`, `role_name`, or `title`. AI client instantiated a new client per extraction without request timeout or Groq JSON schema compliance prompts.
The Context/Why it failed: High-throughput ingestion exposed connection pool contamination, CORS validation failures on credentialed mobile web/fetch clients, and fragility when LLMs omitted optional fields or when LLM API latency spiked.
The Decision/Fix:
1. Wrapped `get_db()` session yield in `try/except Exception: await session.rollback(); raise`.
2. Updated CORS middleware to use `allow_origin_regex=r"^https?://.*"`.
3. In `app/telegram/backfill.py`, fixed 24h backfill to fetch recent messages backwards with a safety cap (`limit=200`), filtering `msg_date >= cutoff`, and aligned message dict schema (`text`, `raw_text`, `date`, `timestamp`, `has_media`).
4. In `app/telegram/listener.py`, cached `self.root_feed_ids` on initialization and tracked background tasks in `self._background_tasks` with done callbacks.
5. In `app/ai/schemas.py`, relaxed `ExtractedEvent` with defaults (`drive_type="FTE"`, `audience_scope="COHORT"`, nullable strings for `company_name`/`role_name`/`title`).
6. In `app/ai/dispatcher.py`, added subchannel-to-company fallback lookup when `company_name` is null, updated `RESCHEDULE_EVENT` to modify both start and end time, and persisted `STATUS_NOTE` to primary role track when unmapped.
7. In `app/ai/client.py` and `extractor.py`, implemented cached `AsyncOpenAI` singleton, added `timeout=30.0`, and updated system prompt with explicit JSON compliance instructions.

[October 2026]: Phase 2 — REST Contracts, Cascading State & Timeline Outer Join
The Problem:
1. `CompanyCreate` threw validation errors when mobile client submitted `company_name` instead of `name`, and lacked optional role fields (`target_role`, `drive_type`, `ctc_stipend_info`), preventing atomic company + role creation.
2. Triage actions (`action == "ELIGIBLE"`) on child `RoleTrack` updated the track but left parent `Company.tracking_mode` unchanged, causing inconsistency.
3. Accessing company stages via `GET /companies/{company_id}/events` returned 500/unhandled if not explicitly guarded with 404 for missing companies.
4. `get_timeline_events()` utilized an `INNER JOIN` on `RoleTrack`, silently dropping company-level events that lacked a `role_track_id` (e.g. general company deadlines or presentations).
5. `TimelineEventResponse` lacked a mirrored `status` field for frontend `display_state` consumers and omitted joined computed properties.
The Context/Why it failed: The mobile app expected unified company/role payload submission on creation, cascading company-level visibility on triage approval, and safe access to company-level events without strict foreign-key joins.
The Decision/Fix:
1. Updated `CompanyCreate` and `CompanyResponse` schemas to support and alias `company_name` and `name`, accepting `target_role`/`role_name`, `drive_type`, and `ctc_stipend_info`. Updated `crud.create_company()` to atomically create the parent `Company` (`tracking_mode="ELIGIBLE"`) and child `RoleTrack` (`is_triaged=True`).
2. Updated `crud.triage_role_track()` to cascade `action == "ELIGIBLE"` to the parent `Company` (`tracking_mode="ELIGIBLE"`) and eagerly reloaded `RoleTrack.company` to eliminate `MissingGreenletError`.
3. Guarded `GET /companies/{company_id}/events` in `app/routers/companies.py` with an existence check returning HTTP 404 if the company does not exist.
4. Refactored `crud.get_timeline_events()` from `INNER JOIN` to `LEFT OUTER JOIN` on `RoleTrack`, allowing company-level events (`role_track_id IS NULL`) for eligible companies to be retrieved across viewports.
5. Synchronized `status` with `display_state` on `TimelineEventResponse` and added computed property fallbacks.

[October 2026]: Phase 3 — Test Suite Hardening & Reality-Check Testing
The Problem:
1. `verify_phase2.py` tested CRUD functions in isolation without exercising the FastAPI HTTP routing layer, leaving mobile payload compatibility (`company_name` vs `name`), 404 existence checks, timeline environment isolation, and event completion HTTP endpoints unverified at the transport level.
2. `verify_phase4.py` tested simplified scenarios but lacked tests for dirty payloads from subchannels where `company_name`, `drive_type`, and `role_name` were omitted, and did not test end-to-end `CREATE_EVENT`, `STATUS_NOTE`, or `RESCHEDULE_EVENT` (both start and end times) dispatch.
3. `verify_triage.py` had fragile inter-test dependencies (`test_fastapi_endpoints` relied on residual state from `test_triage_flow`), did not verify that accepting a role cascades `Company.tracking_mode = "ELIGIBLE"`, and did not test `POST /triage/{id}/ignore` at the HTTP layer.
The Context/Why it failed: Unit-level sanitized mocks allowed architectural regressions to slip past the test suites unnoticed when real payloads or HTTP requests deviated from happy-path assumptions.
The Decision/Fix:
1. In `verify_phase2.py`, added `test_router_http_endpoints()` using `httpx.AsyncClient` with `ASGITransport(app=app)`. Verified `POST /companies/` with mobile payload creating both `Company` and initial `RoleTrack`, `GET /companies/{id}/events` (200 for existing, 404 for missing), `GET /events/timeline?env=MAIN` vs `?env=TEST` environment isolation, and `PATCH /events/{id}/complete` / `POST /events/{id}/uncomplete` state transitions.
2. In `verify_phase4.py`, added 4 hardened test cases:
   - Dirty payload subchannel message missing `company_name`, `drive_type`, and `role_name`, asserting fallback company resolution via `subchannel_id` and default `drive_type = "FTE"`.
   - End-to-end `CREATE_EVENT` dispatch verifying DB persistence and role track association.
   - End-to-end `STATUS_NOTE` dispatch verifying DB write to `latest_status_note`.
   - `RESCHEDULE_EVENT` dispatch asserting update of both `start_time` and `end_time`.
3. In `verify_triage.py`, made `test_fastapi_endpoints()` fully self-contained with isolated fixtures, asserted parent `Company.tracking_mode` promotion from `SILENT_TRACK` to `ELIGIBLE` upon accepting a role track, and tested both `POST /triage/{id}/accept` and `POST /triage/{id}/ignore` endpoints.

[October 2026]: Pre-Interview State Machine Completion, Event Normalization & Fallbacks
The Problem: Real-world Telegram campus announcements exhibit moving deadlines ("till 2pm" -> "extended to 11:59pm"), single timestamps ("OA at 5pm", "Deadline 9pm"), replacement form links, and eligibility criteria relaxations ("6.5+ CGPA now eligible"). The dispatcher previously risked dropping actions if no matching event existed in the database, failed to normalize single timestamps, and did not properly trigger re-triage on criteria relaxation.
The Context/Why it failed: Coordinators frequently post updates for events not previously captured by the listener, or announce events with only a start time or only an end time. If `_find_matching_event` returned `None`, mutations like `EXTEND_DEADLINE`, `RESCHEDULE_EVENT`, and `REPLACE_FORM` silently aborted without creating the event.
The Decision/Fix: Fully completed and hardened the canonical Pre-Interview State Engine (`app/ai/dispatcher.py` and `app/crud.py`):
1. Event Fallback Architecture: `EXTEND_DEADLINE`, `RESCHEDULE_EVENT`, and `REPLACE_FORM` now seamlessly fall back to `crud.create_custom_event` when no existing active event matches in the database.
2. Canonical Time Normalization (`_normalize_event_times`): Live events (`OA_WINDOW`, `PPT`, `INTERVIEW`) with only `start_time` default to `end_time = start_time + 2 hours`. Deadlines (`FORM_SUBMISSION`, `IRIS_DEADLINE`, `CUSTOM`) with only one timestamp set `start_time = end_time = timestamp`.
3. Soft-Delete Filtering: `_find_matching_event` strictly filters `models.TimelineEvent.is_deleted == False`, avoiding ghost matches against soft-deleted records.
4. Form Replacement Supercedence: `REPLACE_FORM` calls `crud.replace_form_event()`, inserting a versioned entry (`version = max_version + 1`), setting both `start_time` and `end_time` to `new_end_time`, updating `action_link`, and forcefully resetting `user_status = "PENDING"`.
5. Criteria Relaxation & Re-Triage: `STATUS_NOTE` calls `crud.flag_for_retriage()`, updating `latest_status_note` and resetting `is_triaged = False` strictly if `tracking_mode == "SILENT_TRACK"`, resurfacing the drive in the mobile Triage Hub without disrupting accepted or ignored tracks.

[October 2026]: Tribal Knowledge 1 — Windows tzdata Crash & Zero-Dependency Native IST Timezone
The Problem: Using `zoneinfo.ZoneInfo("Asia/Kolkata")` crashed fatally on Windows environments with `ZoneInfoNotFoundError: 'No time zone found with key Asia/Kolkata'`.
The Context/Why it failed: Windows does not ship with standard IANA zoneinfo databases. Without the third-party `tzdata` package installed, `zoneinfo` lookup fails on startup or extraction validation.
The Decision/Fix: Replaced `ZoneInfo("Asia/Kolkata")` across `app/ai/prompts.py`, `app/ai/schemas.py`, and `app/ai/rule_extractor.py` with standard library `_IST = timezone(timedelta(hours=5, minutes=30), name="IST")`. Because Indian Standard Time is strictly fixed at UTC+05:30 with zero Daylight Saving Time, this native definition is 100% deterministic, cross-platform safe, and immune to missing system timezone files. Additionally added `tzdata>=2024.1` as a defensive dependency in `requirements.txt`.

[October 2026]: Tribal Knowledge 2 — Groq Token Headroom (max_tokens=4096) & Dual-Tier Fallback Extraction
The Problem: Groq extraction calls failed with HTTP 400 `json_validate_failed` or `EOF while parsing a string` when parsing long or multi-action announcements.
The Context/Why it failed: Setting `max_tokens=1024` caused Groq LPU inference to truncate output mid-string when emitting multi-event JSON payloads. When token limits cut off the output before the closing `}`, both Pydantic and JSON parsers crashed.
The Decision/Fix:
1. Increased `max_tokens=4096` in `app/ai/extractor.py` on both the primary completions call and the fallback retry.
2. Implemented an in-flight fallback parser that retries on `json_validate_failed` without `json_object` schema constraints and uses regex to extract the first valid JSON block while stripping markdown code fences.
3. Added `app/ai/rule_extractor.py` as a deterministic regex fallback that parses canonical relative IST recruitment announcements ("tomorrow 9pm", "today by 11:59 PM", "OA window 8-10am") if the LLM produces 0 actions or fails inference.

[October 2026]: Tribal Knowledge 3 — Test Database Hygiene & In-Memory SQLite Isolation (:memory:)
The Problem: Test fixtures running against local database files polluted the `MAIN` feed environment with mock companies (`FallbackTestCorp`, `Acme Corp`), corrupting production timeline feeds.
The Context/Why it failed: Sharing `dev.db` between unit tests and the live server allowed mock data to persist and be served to mobile clients connecting to the backend.
The Decision/Fix: Enforced that all test verification suites (`verify_phase1.py` through `verify_phase4.py`, `verify_triage.py`, `verify_settings.py`) strictly bind to isolated `sqlite+aiosqlite:///:memory:` engines. Created `scripts/clean_test_data.py` as an administrative hygiene tool to purge test records from `MAIN` and maintain pristine separation between `MAIN` and `TEST` environments.

[October 2026]: Principal Engineering Review, Documentation Corrections & Tracking-Status Invariants (2026-10-22)
The Problem: A read-only architectural review (2026-10-21) found that several status claims in this journal and in PROJECT_STATE.md did not match the code, and that ingestion, extraction and storage fail silently in ways the verification suites never exercised.
The Context/Why it failed: Documentation recorded intended behaviour rather than verified behaviour. The verify_* suites mock the LLM and contain no realistic multi-sentence announcements, so spec gaps and regressions passed as green.
The Decision/Fix:
1. Corrected claims (the code is the source of truth; earlier entries are kept as written):
   - "[September 2026] Missed In-Place Schedule Modifications": edits are re-extracted as new text. No message provenance exists (no message-id column or lookup), so an edit can create a duplicate event rather than update the original.
   - "[October 2026] Tribal Knowledge 2": the regex tier also runs when the LLM validly returns zero actions, and was verified to create wrong events ("Round 2 of the OA is tomorrow at 5pm" → 02:00; "OA on 25th October at 5 PM" → today 17:00; a defaulter-list message → a FORM deadline). It is scheduled for removal in Phase 0.5.
   - "[October 2026] Tribal Knowledge 3": verify_phase3.py binds no in-memory engine and reads the configured database through `_is_known_subchannel` (fix in Phase 0.2). dev.db was never committed; commit 09cbaa5 only added ignore rules. `scripts/clean_test_data.py` hard-deletes MAIN companies by substring match and must not be run against real data until Phase 0.6.
   - Sessions: when TELEGRAM_SESSION_STRING is empty, the client uses the file session `placement_session.session` (present on disk, git-ignored); "no .session files on disk" was inaccurate.
   - Storage: SQLite drops datetime offsets without converting, so IST- and UTC-written rows coexist and `display_state` mis-reads IST rows by 5.5 h (reproduced on an in-memory database).
2. Recorded the verified defects as PROJECT_STATE.md §0.2 (Known Gaps C1–C7, H1–H9, M1–M7).
3. Recorded the owner-defined tracking-status invariants as PROJECT_STATE.md §0.1: IGNORED is a terminal, human-only status whose channels are not processed (only `PATCH /companies/{id}/tracking` can reverse it, followed by a bounded 24-hour backfill); criteria relaxation applies only to SILENT_TRACK and only proposes re-triage — never auto-promotion; CGPA/branch are never given to the automation.
4. Started the approved remediation plan "Phase 0 — stop silent losses" on branch `feature/phase-0-stop-losses`. Production will use Python 3.12, so code must stay 3.12-compatible.

[October 2026]: Phase 0.2 — Alembic Schema Ownership, UTC Datetime Storage & Strict Test Databases (2026-10-23)
The Problem: The schema was created by `create_all()` plus two error-suppressed `ALTER TABLE` statements at startup, which would silently roll back the whole schema on PostgreSQL. SQLite stored IST- and UTC-written datetimes in the same columns with no offset, so `display_state` mis-read IST deadlines by 5.5 h. Company names were globally unique (a TEST company could not share a MAIN company's name). SQLite ignored foreign keys, so tests could not catch violations PostgreSQL would reject, and `verify_phase3.py` could read the configured database.
The Context/Why it failed: `create_all()` cannot evolve an existing schema, and the suppressed ALTERs only worked because SQLite DDL errors are harmless there. SQLAlchemy's SQLite DATETIME writes the wall-clock fields of a tz-aware value as-is, discarding the offset. SQLite also enforces foreign keys only when `PRAGMA foreign_keys=ON` is set per connection.
The Decision/Fix:
1. Added Alembic 1.20.1 (`alembic.ini`, async `alembic/env.py`, baseline revision `0001`). `env.py` reads the app's `DATABASE_URL`, renders the custom datetime type as plain `sa.DateTime(timezone=True)` (migration files never import app code), uses batch mode for SQLite, and accepts a caller-supplied connection so tests can migrate in-memory databases. A `NAMING_CONVENTION` on `Base.metadata` gives every constraint a deterministic name.
2. Removed `create_all()` and the ALTERs from the lifespan. `assert_schema_current()` now refuses to start the app unless the database is at the head revision, with an explicit "run alembic upgrade head" message.
3. Added the `UTCDateTime` column type (aware → UTC on write, aware UTC on read) for every datetime column, plus the `as_utc()` policy: a naive datetime is assumed UTC and raises `NaiveDatetimeWarning`. API request schemas now require offsets (`AwareDatetime`). The dispatcher shows schedule times to the LLM in IST, so the prompt is unchanged by the storage switch.
4. Schema additions for later sub-phases: `UNIQUE (normalized_name, environment_type)` on companies, `companies.invite_token`, `role_tracks.latest_note_kind`, `timeline_events.needs_review / review_reason / source_chat_id / source_message_id`, and the `message_ledger` and single-row `worker_status` tables.
5. SQLite connections now get WAL journaling, a 5 s busy timeout and `foreign_keys=ON` (`install_sqlite_pragmas`). Every verify suite applies the same pragmas to its in-memory engine and turns `NaiveDatetimeWarning` into an error; `verify_phase3.py` now binds `:memory:`. Added `verify_schema.py` (migration round trip + drift check, startup check, UTC round trip, deadline flips to MISSED on time, uniqueness, foreign keys, ledger/status constraints).
6. Cold start: the old `dev.db` (TEST data only) was renamed to `dev.db.pre-phase0.bak` and recreated with `alembic upgrade head`. The fresh database has no `system_config` row, so the profile and feed IDs must be re-entered in Settings.
Verification: all 7 suites green on `:memory:`; the alembic CLI upgrade → downgrade → upgrade → check is clean on a scratch file; PostgreSQL DDL was rendered offline (TIMESTAMP WITH TIME ZONE, BOOLEAN, named constraints, no invalid defaults). A live PostgreSQL run wasn't possible locally (no Docker) and is the first step on the VM in Phase 1.
Follow-up (2026-10-23): at the owner's request, the single `system_config` row (profile + feed IDs) was copied from `dev.db.pre-phase0.bak` into the new `dev.db` (read-only ATTACH of the backup). `main_feed_id` was already empty in the backup.

[October 2026]: Phase 0.3 — API / Ingestion Worker Split, Message Ledger, Catch-up & Retries (2026-10-23)
The Problem: Telethon ran inside the FastAPI process, so a second uvicorn worker — or the laptop and a server on the same session — would get the personal session revoked (`AUTH_KEY_DUPLICATED`). The listener task was unsupervised and `/health` always reported healthy. Messages posted while the process was down, still buffered at a crash, or in a batch whose dispatch failed were lost for good. Every message from an unmonitored chat cost a database query, and IGNORED/deleted companies' channels could be re-added by that fallback.
The Context/Why it failed: Ingestion had no persistent record of what it had processed, so there was nothing to resume from or retry. The dispatcher bridge swallowed exceptions. Monitoring decisions were made per message instead of from the database's tracking state.
The Decision/Fix:
1. `app/main.py` is now API-only (no Telethon, listener or dispatcher imports). `app/worker.py` (`python -m app.worker`) owns the Telegram client and must run as exactly one instance per session.
2. `TELEGRAM_API_ID=0` (or an empty API hash) is enforced twice: the worker exits 0 before touching Telegram or the database, and `create_telegram_client()` refuses to build a client.
3. New `app/telegram/ledger.py` over `message_ledger`. Pre-filtered chatter and invite posts are `SKIPPED` immediately; dispatched batches are `DONE`; a raising payload handler marks them `FAILED`, retried after 1 m → 5 m → 30 m → 2 h and given up after 6 attempts. Stored error text is limited to the exception class and first line, so SQL parameters and message text stay out.
4. Monitored set: `crud.get_ingestible_subchannel_ids()` implements invariants 2–3 (active, ingested environment, not IGNORED at company level, not every role IGNORED). The worker refreshes it every 60 s: chats leaving the set have their buffers discarded; chats joining it (un-ignore, new feed in Settings) get a bounded 24 h catch-up (invariant 4). The per-message `_is_known_subchannel` fallback was removed, and joins only monitor and backfill ingestible companies.
5. Catch-up runs at boot and every 15 min. It re-scans from 100 messages behind each chat's ledger cursor, because a crash can leave buffered messages below a later recorded one. It is bounded by 72 h (24 h for chats without history), skips anything recorded, buffered or in flight, and routes the rest like live messages. Backfills skip ledger-known messages, so re-posted invites no longer re-process a channel. Chats not yet in the session's entity cache are skipped until a live update caches them; no `get_dialogs()` call is made, which would list personal chats.
6. The worker writes a heartbeat into `worker_status` every 30 s (state, last update, last successful dispatch, last catch-up, last error), optionally pings `DEADMAN_URL` every 5 min while connected, and exits 1 when the listener stops or 2 on session errors (after a 10-minute wait so a supervisor doesn't hammer Telegram). SIGTERM flushes buffers (30 s cap) and exits 0.
7. Config: `INGEST_ENVIRONMENTS` (default `TEST`, so a development machine never reads the real feed; production sets `MAIN,TEST`), loop intervals, `CATCHUP_MAX_AGE_HOURS`, `DEADMAN_URL`. The `DATABASE_URL` default is now the local `dev.db` instead of a URL with a placeholder credential. `API_KEY` and `DEBOUNCE_MAX_WAIT_SECONDS` will be added in 0.6 and 0.4, where they're enforced.
8. Known limits until later sub-phases: the extractor still swallows LLM errors, so they don't reach the retry loop (0.5); caught-up messages still get relative dates resolved against processing time (0.5); edits made while the worker was down aren't replayed; invite-post joins aren't retried (0.4).
Verification: 8 suites green on `:memory:`, including the new `verify_worker.py` (10 checks; the real `TelegramClient` class is replaced by one that fails if constructed). `python -m app.worker` with `TELEGRAM_API_ID=0` exits 0 without connecting; the API boots against `dev.db`, serves the restored settings and logs no Telegram activity.
