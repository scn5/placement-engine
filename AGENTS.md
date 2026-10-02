# AGENT RULES: PLACEMENT ENGINE (BACKEND)

## 1. Core Framework Constraints
* **Runtime:** Python 3.14. 
* **Framework:** FastAPI >= 0.111.0. All endpoint functions must be `async def`.
* **Validation:** Pydantic V2. Use `model_validate()` and `model_dump()`. Do NOT use deprecated V1 methods like `parse_obj()` or `dict()`.

## 2. Database & ORM (CRITICAL)
* **SQLAlchemy 2.0:** Strictly use the 2.0 style syntax (e.g., `select(Company).where(...)`). Do not use legacy Query API (`session.query()`).
* **Async Drivers:** Use `asyncpg` for PostgreSQL and `aiosqlite` for local dev. Never use synchronous `psycopg2`.
* **Sessions:** Every database operation must use the `AsyncSessionLocal` factory and close cleanly.

## 3. External Integrations
* **Telethon:** The Telegram ingestion listener runs on `asyncio`. Ensure no blocking synchronous code (like `time.sleep` or heavy synchronous CPU bounds) is executed inside the event handlers, as this drops MTProto keep-alives.
* **LLM Engine:** Use the `openai` Python SDK configured with a custom `base_url` for Groq. Always pass `response_format={"type": "json_object"}`.

## 4. Tenancy & Auth
* **Single Tenant:** Do not build multi-user authentication, JWT middleware, or user tables. This system belongs to a single user. Trust the incoming requests from the paired frontend.