"""
verify_security.py
Phase 0.6 — API security & hardening verification suite.

Covers:
  • every route except /health requires X-API-Key (checked in the OpenAPI schema
    and by calling every operation without a key); constant-time comparison
  • /health is public and minimal; GET /status reports the worker's heartbeat
  • no CORS headers; docs and OpenAPI off in production (APP_ENV fail-closed)
  • production refuses to start without a strong API_KEY
  • missing references → 404/422 and constraint violations → 409/422, never a
    500 or SQL text
  • scripts: login_telegram.py prints no session string (checked statically —
    never run); clean_test_data.py is dry-run by default and TEST-only
  • .dockerignore keeps secrets, sessions, databases, venv/ and .git/ out of images

Every database is an in-memory SQLite instance; nothing touches the network.
Run: python verify_security.py
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import ast
import asyncio
import importlib.util
import inspect
import logging
import re
import secrets
import sqlite3
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# Patch DB engine BEFORE importing app modules (same pattern as Phase 2/4)
# ---------------------------------------------------------------------------
import app.database as _db_module

_test_engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
_TestSession = async_sessionmaker(bind=_test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
_db_module.engine = _test_engine
_db_module.AsyncSessionLocal = _TestSession
_db_module.install_sqlite_pragmas(_test_engine)  # enforce foreign keys like production

from alembic import command
from alembic.config import Config

import app.main as main_module
from app.config import Settings, settings
from app.database import get_db
from app.models import Company, NaiveDatetimeWarning, RoleTrack, TimelineEvent, WorkerStatus
from app.security import MIN_API_KEY_LENGTH, check_api_key_configuration, require_api_key

# Every datetime handed to the ORM must be timezone-aware.
warnings.simplefilter("error", NaiveDatetimeWarning)

ROOT = Path(__file__).resolve().parent
KEY = secrets.token_urlsafe(32)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _override_get_db():
    async with _TestSession() as session:
        yield session


def _client(app) -> AsyncClient:
    app.dependency_overrides[get_db] = _override_get_db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _Settings:
    """Temporarily change settings, restoring them afterwards."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for name, value in self.values.items():
            self.saved[name] = getattr(settings, name)
            setattr(settings, name, value)

    def __exit__(self, *exc_info):
        for name, value in self.saved.items():
            setattr(settings, name, value)


def _migrate(connection) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["connection"] = connection
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_every_route_requires_the_key() -> None:
    app = main_module.app
    operations = [
        (method.upper(), path, operation)
        for path, item in app.openapi()["paths"].items()
        for method, operation in item.items()
    ]
    assert len(operations) >= 20, len(operations)
    for method, path, operation in operations:
        if path == "/health":
            assert "security" not in operation, "/health must stay public"
        else:
            assert operation.get("security") == [{"APIKeyHeader": []}], (method, path)
    assert "compare_digest" in inspect.getsource(require_api_key)

    with _Settings(API_KEY=SecretStr(KEY)):
        async with _client(app) as client:
            for method, path, _ in operations:
                url = re.sub(r"\{[^}]+\}", "1", path)
                for headers in ({}, {"X-API-Key": "wrong-key"}, {"X-API-Key": KEY[:-1]}):
                    response = await client.request(method, url, headers=headers)
                    if path == "/health":
                        assert response.status_code == 200 and response.json() == {"status": "ok"}
                    else:
                        assert response.status_code == 401, (method, url, headers, response.status_code)
                        assert response.json() == {"detail": "Missing or invalid API key."}
            for headers in ({"X-API-Key": KEY}, {"x-api-key": KEY}):
                assert (await client.get("/companies/", headers=headers)).status_code == 200
    app.dependency_overrides.clear()

    print(f"  [1] ✓ All {len(operations) - 1} operations except /health require X-API-Key (schema and live calls: missing, wrong, truncated → 401); constant-time compare")


async def test_health_and_status() -> None:
    app = main_module.app
    headers = {"X-API-Key": KEY}
    with _Settings(API_KEY=SecretStr(KEY)):
        async with _client(app) as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            assert (await client.get("/status")).status_code == 401

            body = (await client.get("/status", headers=headers)).json()
            assert body["status"] == "unknown" and body["telegram_state"] is None

            async with _TestSession() as db:
                db.add(WorkerStatus(telegram_state="CONNECTED", heartbeat_at=_now() - timedelta(seconds=10),
                                    last_llm_ok_at=_now() - timedelta(minutes=3), last_error="RuntimeError: boom"))
                await db.commit()
            body = (await client.get("/status", headers=headers)).json()
            assert body["status"] == "ok" and body["telegram_state"] == "CONNECTED"
            assert 5 <= body["heartbeat_age_seconds"] <= 30 and body["last_error"] == "RuntimeError: boom"

            async with _TestSession() as db:
                row = await db.get(WorkerStatus, 1)
                row.heartbeat_at = _now() - timedelta(minutes=10)  # three heartbeats missed
                await db.commit()
            assert (await client.get("/status", headers=headers)).json()["status"] == "degraded"

            async with _TestSession() as db:
                row = await db.get(WorkerStatus, 1)
                row.heartbeat_at, row.telegram_state = _now(), "DISCONNECTED"
                await db.commit()
            assert (await client.get("/status", headers=headers)).json()["status"] == "degraded"
    app.dependency_overrides.clear()

    print("  [2] ✓ /health is public and returns only {\"status\": \"ok\"}; /status (authenticated) reports ok / degraded / unknown from worker_status")


async def test_no_cors_and_docs_off_in_production() -> None:
    app = main_module.app
    assert not [m for m in app.user_middleware if "CORS" in getattr(m.cls, "__name__", "")]
    async with _client(app) as client:
        origin = {"Origin": "https://evil.example"}
        preflight = await client.options(
            "/companies/", headers={**origin, "Access-Control-Request-Method": "GET"}
        )
        assert "access-control-allow-origin" not in preflight.headers
        assert "access-control-allow-origin" not in (await client.get("/health", headers=origin)).headers
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await client.get(path)).status_code == 200, path  # development keeps them
    app.dependency_overrides.clear()

    with _Settings(APP_ENV="production", API_KEY=SecretStr(KEY)):
        production_app = main_module.create_app()
        async with _client(production_app) as client:
            for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
                assert (await client.get(path)).status_code == 404, path
            assert (await client.get("/companies/", headers={"X-API-Key": KEY})).status_code == 200

    for value in ("production", "prod", "staging", " PRODUCTION "):
        assert Settings(_env_file=None, APP_ENV=value).is_production, value
    for value in ("development", "testing", "Development"):
        assert not Settings(_env_file=None, APP_ENV=value).is_production, value

    print("  [3] ✓ No CORS headers (preflight or simple requests); /docs, /redoc, /openapi.json off in production; any APP_ENV but development/testing counts as production")


async def test_production_refuses_to_start_without_a_key() -> None:
    app = main_module.app
    for key, fragment in ((None, "API_KEY is not set"), (SecretStr("   "), "API_KEY is not set"),
                          (SecretStr("short-key"), f"shorter than {MIN_API_KEY_LENGTH}")):
        with _Settings(APP_ENV="production", API_KEY=key):
            try:
                async with app.router.lifespan_context(app):
                    raise AssertionError("the API started in production without a valid key")
            except RuntimeError as exc:
                assert fragment in str(exc), str(exc)
                assert "short-key" not in str(exc)  # the key itself is never echoed
    with _Settings(APP_ENV="production", API_KEY=SecretStr(KEY)):
        check_api_key_configuration()

    # Should a production server run without a key anyway, it rejects every request.
    with _Settings(APP_ENV="production", API_KEY=None):
        async with _client(app) as client:
            response = await client.get("/companies/")
            assert response.status_code == 503 and "no API key" in response.json()["detail"]
    app.dependency_overrides.clear()

    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logging.getLogger("app.security").addHandler(handler)
    try:
        with _Settings(APP_ENV="development", API_KEY=None):
            check_api_key_configuration()
    finally:
        logging.getLogger("app.security").removeHandler(handler)
    assert any("every API route is open" in r.getMessage() for r in records)

    print("  [4] ✓ Production refuses to start with a missing, blank or short API_KEY (and would answer 503); development without a key starts with a warning")


async def test_missing_references_and_constraints() -> None:
    app = main_module.app
    headers = {"X-API-Key": KEY}
    async with _TestSession() as db:
        alpha = Company(name="Ref Alpha", normalized_name="ref alpha")
        beta = Company(name="Ref Beta", normalized_name="ref beta")
        db.add_all([alpha, beta])
        await db.flush()
        beta_role = RoleTrack(company_id=beta.id, role_name="SDE")
        db.add(beta_role)
        await db.commit()
        alpha_id, beta_role_id = alpha.id, beta_role.id

    event = {"event_type": "OA_WINDOW", "title": "OA", "end_time": (_now() + timedelta(days=1)).isoformat()}
    with _Settings(API_KEY=SecretStr(KEY)):
        async with _client(app) as client:
            cases = [
                ("POST", "/events/", {**event, "company_id": 999999}, 422, "company_id 999999 does not exist"),
                ("POST", "/events/", {**event, "company_id": alpha_id, "role_track_id": beta_role_id}, 422, "is not a role of company"),
                ("POST", "/companies/999999/tracks", {"company_id": 999999, "role_name": "SDE"}, 404, "not found"),
                ("POST", "/events/companies/999999/replace-form",
                 {"new_form_link": "https://forms.gle/x", "new_end_time": event["end_time"]}, 404, "not found"),
                ("POST", "/companies/", {"name": "Ref Alpha"}, 409, "already exists"),
            ]
            for method, url, body, code, fragment in cases:
                response = await client.request(method, url, json=body, headers=headers)
                assert response.status_code == code, (url, response.status_code, response.text)
                assert fragment in response.json()["detail"], (url, response.text)
                assert "INSERT" not in response.text and "SELECT" not in response.text
            response = await client.post(f"/events/companies/{alpha_id}/replace-form?role_track_id={beta_role_id}",
                                         json={"new_form_link": "https://forms.gle/x", "new_end_time": event["end_time"]},
                                         headers=headers)
            assert response.status_code == 422
    app.dependency_overrides.clear()

    def integrity(orig):
        return IntegrityError("INSERT INTO companies (name) VALUES (?)", ("secret",), orig)

    request = SimpleNamespace(method="POST", url=SimpleNamespace(path="/x"))
    sqlite_fk = integrity(sqlite3.IntegrityError("FOREIGN KEY constraint failed"))
    postgres_unique = integrity(type("PgError", (Exception,), {"sqlstate": "23505"})("duplicate key value"))
    other = integrity(sqlite3.IntegrityError("NOT NULL constraint failed: companies.name"))
    for exc, code in ((sqlite_fk, 422), (postgres_unique, 409), (other, 422)):
        response = await main_module._integrity_error(request, exc)
        assert response.status_code == code
        assert b"secret" not in response.body and b"INSERT" not in response.body

    print("  [5] ✓ Missing records → 404 (path) / 422 (body); duplicates → 409; any other constraint violation → 422 — never a 500 or SQL text")


def test_login_script_prints_no_session_string() -> None:
    source = (ROOT / "scripts" / "login_telegram.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    strings = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    assert "StringSession" not in names and "StringSession" not in attributes
    assert "save" not in attributes  # no session export of any kind
    assert not [s for s in strings if "TELEGRAM_SESSION_STRING=" in s]

    print("  [6] ✓ scripts/login_telegram.py exports and prints no session string (checked statically; the script is never run)")


async def test_clean_test_data_is_dry_run_and_test_only() -> None:
    spec = importlib.util.spec_from_file_location("clean_test_data", ROOT / "scripts" / "clean_test_data.py")
    cleaner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cleaner)

    async with _TestSession() as db:
        main_co = Company(name="Acme Test Corp", normalized_name="acme test corp", environment_type="MAIN")
        alpha = Company(name="Sandbox Alpha", normalized_name="sandbox alpha", environment_type="TEST")
        beta = Company(name="Sandbox Beta", normalized_name="sandbox beta", environment_type="TEST")
        db.add_all([main_co, alpha, beta])
        await db.flush()
        for company in (main_co, alpha):
            role = RoleTrack(company_id=company.id, role_name="SDE")
            db.add(role)
            await db.flush()
            db.add(TimelineEvent(company_id=company.id, role_track_id=role.id, event_type="OA_WINDOW",
                                 title=f"{company.name} OA", end_time=_now() + timedelta(days=1)))
        await db.commit()
        ids = {"main": main_co.id, "alpha": alpha.id, "beta": beta.id}

    async def remaining():
        async with _TestSession() as db:
            names = (await db.execute(select(Company.name).where(Company.name.in_(
                ["Acme Test Corp", "Sandbox Alpha", "Sandbox Beta"])))).scalars().all()
            events = (await db.execute(select(TimelineEvent.company_id))).scalars().all()
            roles = (await db.execute(select(RoleTrack.company_id))).scalars().all()
            return sorted(names), events, roles

    listed = await cleaner.run()  # dry run by default
    assert sorted(c.name for c in listed if c.name.startswith("Sandbox")) == ["Sandbox Alpha", "Sandbox Beta"]
    assert all(c.environment_type == "TEST" for c in listed) and "Acme Test Corp" not in [c.name for c in listed]
    assert (await remaining())[0] == ["Acme Test Corp", "Sandbox Alpha", "Sandbox Beta"]

    assert [c.name for c in await cleaner.run(name_filter="beta")] == ["Sandbox Beta"]
    await cleaner.run(apply=True, name_filter="alpha")
    names, events, roles = await remaining()
    assert names == ["Acme Test Corp", "Sandbox Beta"]
    assert ids["alpha"] not in events and ids["alpha"] not in roles and ids["main"] in events

    async with _TestSession() as db:
        main_company = await db.get(Company, ids["main"])
        try:
            await cleaner.delete_companies(db, [main_company])
        except ValueError:
            pass
        else:
            raise AssertionError("a MAIN company was deleted")

    print("  [7] ✓ scripts/clean_test_data.py: dry run by default, TEST companies only (MAIN 'Acme Test Corp' untouched), --apply deletes roles and events too")


def _docker_excluded(path: str, patterns) -> bool:
    """Approximation of Docker's .dockerignore matching (later patterns win)."""
    excluded = False
    parts = path.split("/")
    candidates = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    for pattern in patterns:
        negate = pattern.startswith("!")
        body = pattern[1:] if negate else pattern
        body = body.rstrip("/")
        regex = ""
        i = 0
        while i < len(body):
            if body.startswith("**/", i):
                regex += "(?:.*/)?"
                i += 3
            elif body[i] == "*":
                regex += "[^/]*"
                i += 1
            elif body[i] == "?":
                regex += "[^/]"
                i += 1
            elif body[i] == "[":
                end = body.index("]", i)
                regex += body[i:end + 1]
                i = end + 1
            else:
                regex += re.escape(body[i])
                i += 1
        if any(re.fullmatch(regex, candidate) for candidate in candidates):
            excluded = not negate
    return excluded


def test_dockerignore() -> None:
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    patterns = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    for required in (".env*", "*.session*", "*.db*", "venv/", ".git/"):
        assert required in patterns, required
    excluded = [
        ".env", ".env.example", ".env.production", "app/.env", "placement_session.session",
        "placement_session.session-journal", "dev.db", "dev.db-wal", "dev.db.pre-phase0.bak",
        "venv/Lib/site.py", ".git/HEAD", "app/__pycache__/main.cpython-312.pyc", "worker.log",
    ]
    kept = [
        "app/main.py", "app/database.py", "app/telegram/client.py", "alembic/env.py", "alembic.ini",
        "requirements.txt", "scripts/login_telegram.py", "verify_security.py",
    ]
    for path in excluded:
        assert _docker_excluded(path, patterns), f"{path} would be copied into the image"
    for path in kept:
        assert not _docker_excluded(path, patterns), f"{path} would be left out of the image"

    print("  [8] ✓ .dockerignore keeps .env*, sessions, databases and backups, venv/, .git/ and caches out of the build context; code stays in")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def main() -> None:
    async with _test_engine.begin() as conn:
        await conn.run_sync(_migrate)

    print("\nRunning Phase 0.6 API security & hardening verification suite…\n")

    await test_every_route_requires_the_key()
    await test_health_and_status()
    await test_no_cors_and_docs_off_in_production()
    await test_production_refuses_to_start_without_a_key()
    await test_missing_references_and_constraints()
    test_login_script_prints_no_session_string()
    await test_clean_test_data_is_dry_run_and_test_only()
    test_dockerignore()

    await _test_engine.dispose()
    print("\n✅ Phase 0.6 API Security & Hardening Verified Successfully!")


if __name__ == "__main__":
    asyncio.run(main())
