"""
verify_settings.py
Verifies the Phase 5 System Config/Settings API implementation.
"""
import sys
import asyncio
from datetime import datetime, timezone

import httpx

sys.stdout.reconfigure(encoding="utf-8")

# Monkey-patch settings for test environment
from app.config import settings
settings.DEBOUNCE_DELAY_SECONDS = 0.1
settings.TELEGRAM_API_ID = 0

# Monkey-patch database to use in-memory SQLite before importing app
import app.database as _db_module
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

_test_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    echo=False,
    connect_args={"check_same_thread": False},
)
_TestSession = async_sessionmaker(
    bind=_test_engine,
    autocommit=False, 
    autoflush=False,
)
_db_module.engine = _test_engine
_db_module.AsyncSessionLocal = _TestSession
_db_module.install_sqlite_pragmas(_test_engine)  # enforce foreign keys like production

import warnings

from app.main import app
from app.models import Base, NaiveDatetimeWarning
from app.database import get_db

# Every datetime handed to the ORM must be timezone-aware.
warnings.simplefilter("error", NaiveDatetimeWarning)

async def verify():
    print("🚀 Running Phase 5 (Settings API) Verification Suite...\n")
    
    # 1. Bootstrap DB
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Database schema created.")

    async def override_get_db():
        async with _TestSession() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    from app.security import require_api_key  # authentication: verify_security.py
    app.dependency_overrides[require_api_key] = lambda: None

    # We need to use httpx.ASGITransport for testing FastAPI app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 2. Test GET when empty (should return 404)
        print("\n--- Test GET /settings (Empty) ---")
        resp = await client.get("/settings")
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"
        print("✅ GET /settings returns 404 when no config exists.")

        # 3. Test PUT (Create)
        print("\n--- Test PUT /settings (Create) ---")
        payload = {
            "student_name": "John Doe",
            "roll_number": "211CS111",
            "personal_email": "john@example.com",
            "college_email": "john.211cs111@nitk.edu.in",
            "main_feed_id": -100123456789,
            "test_feed_id": -100987654321
        }
        resp = await client.put("/settings", json=payload)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["student_name"] == "John Doe"
        assert data["roll_number"] == "211CS111"
        assert "id" in data
        assert "updated_at" in data
        print("✅ PUT /settings creates a new system config.")

        # 4. Test GET after Create
        print("\n--- Test GET /settings (Exists) ---")
        resp = await client.get("/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["student_name"] == "John Doe"
        print("✅ GET /settings returns the created config.")

        # 5. Test PUT (Update/Partial)
        print("\n--- Test PUT /settings (Partial Update) ---")
        update_payload = {
            "student_name": "Jane Doe",
            "roll_number": "211CS112"
        }
        resp = await client.put("/settings", json=update_payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["student_name"] == "Jane Doe"
        assert data["roll_number"] == "211CS112"
        assert data["personal_email"] == "john@example.com" # Should remain untouched
        print("✅ PUT /settings correctly updates existing config partially.")

    print("\n🎉 All Phase 5 checks passed successfully!")

if __name__ == "__main__":
    asyncio.run(verify())
