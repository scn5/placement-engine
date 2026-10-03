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