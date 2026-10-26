# AGENT RULES: PLACEMENT ENGINE (BACKEND)

## 1. Core Framework Constraints
* **Runtime:** Code must run on **Python 3.12** (the production image is `python:3.12-slim`). The local dev venv is 3.14, so avoid 3.13+/3.14-only syntax and standard-library APIs — notably unquoted forward-reference annotations that only work under 3.14's deferred annotation evaluation.
* **Framework:** FastAPI >= 0.111.0. All endpoint functions must be `async def`.
* **Validation:** Pydantic V2. Use `model_validate()` and `model_dump()`. Do NOT use deprecated V1 methods like `parse_obj()` or `dict()`.

## 2. Database & ORM (CRITICAL)
* **SQLAlchemy 2.0:** Strictly use the 2.0 style syntax (e.g., `select(Company).where(...)`). Do not use legacy Query API (`session.query()`).
* **Async Drivers:** Use `asyncpg` for PostgreSQL and `aiosqlite` for local dev. Never use synchronous `psycopg2`.
* **Sessions:** Every database operation must use the `AsyncSessionLocal` factory and close cleanly.

## 3. External Integrations
* **Telethon:** The Telegram ingestion listener runs on `asyncio`. Ensure no blocking synchronous code (like `time.sleep` or heavy synchronous CPU bounds) is executed inside the event handlers, as this drops MTProto keep-alives.
* **LLM Engine:** Use the `openai` Python SDK configured with a custom `base_url` for Groq. Request Groq strict structured output (`json_schema`, `strict: true`, `RESULT_JSON_SCHEMA`) with the JSON-object fallback, and keep that schema in step with `ExtractedEvent` / `LLMExtractionResult` (`verify_extraction.py` checks it). Never add a regex or heuristic extraction fallback that writes events — a failed extraction raises `ExtractionFailed` and is retried, never guessed (see `PROJECT_STATE.md` Tribal Knowledge #2).

## 4. Tenancy & Auth
* **Single Tenant:** Do not build multi-user authentication, JWT middleware, or user tables. This system belongs to a single user. The only authentication is one shared API key: every router is included with `Depends(require_api_key)` (`app/security.py`) and only `/health` is public — keep it that way for new routes (`verify_security.py` checks every operation). Never log or echo the key.

## 5. Binding Domain Rules
* **Tracking-status invariants:** `PROJECT_STATE.md` §0.1 is binding. In short: `IGNORED` is a terminal, human-only status (only `PATCH /companies/{id}/tracking` reverses it, and its channel is not processed); criteria relaxation only *proposes* re-triage for `SILENT_TRACK` roles and never changes `tracking_mode`; CGPA/branch are never given to the automation.
* **Telegram safety:** Never run code against the live session from tests or agent sessions; mock Telethon instead. Only the ingestion worker (`python -m app.worker`) talks to Telegram: never start it with `TELEGRAM_API_ID` set — use a process-level `TELEGRAM_API_ID=0` override and never edit `.env`. Never run two workers on one session. The API (`app/main.py`) must not import Telethon, `app.telegram` or `app.worker` (enforced by `verify_worker.py`). Auto-join may only follow the join policy (`app/telegram/join_policy.py`, tested in `verify_ingestion.py`): links only from root-feed posts; channels and supergroups only — never persons, bots, basic groups, permalinks, root feeds or approval-only invites. Never subscribe to reaction updates or read `message.reactions`: only message text and captions are extracted. Test with mocked entities only — never resolve a live link.