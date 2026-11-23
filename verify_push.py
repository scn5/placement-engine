"""
verify_push.py
Verifies push device registration and delivery (Phase 1.4, step 1):
POST /devices/register, POST /notify/test, the Expo client and token cleanup.

Every database is in-memory and the Expo Push API is never contacted: the HTTP
layer is a stub transport, and the endpoint tests replace send_expo_push.
"""
import asyncio
import sys
import warnings

import httpx

sys.stdout.reconfigure(encoding="utf-8")

from app.config import settings
settings.TELEGRAM_API_ID = 0

# Bind the app to an in-memory database before importing it.
import app.database as _db_module
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

_engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
_Session = async_sessionmaker(bind=_engine, autocommit=False, autoflush=False, expire_on_commit=False)
_db_module.engine = _engine
_db_module.AsyncSessionLocal = _Session
_db_module.install_sqlite_pragmas(_engine)

from app import models, push
from app.database import get_db
from app.main import app
from app.models import Base, NaiveDatetimeWarning
from app.security import require_api_key

# Every datetime handed to the ORM must be timezone-aware.
warnings.simplefilter("error", NaiveDatetimeWarning)

_TOKEN_A = "ExponentPushToken[aaaaaaaaaaaaaaaaaaaaaa]"
_TOKEN_B = "ExponentPushToken[bbbbbbbbbbbbbbbbbbbbbb]"


async def _tokens() -> list[str]:
    async with _Session() as db:
        rows = (await db.execute(select(models.DeviceToken.expo_push_token))).scalars().all()
    return sorted(rows)


class _Expo:
    """Replaces push.send_expo_push: records the messages, answers per token."""

    def __init__(self, gone: set[str] = frozenset(), fail: set[str] = frozenset()):
        self.gone, self.fail, self.calls = gone, fail, []

    async def __call__(self, messages):
        self.calls.append(messages)
        tickets = []
        for m in messages:
            if m["to"] in self.gone:
                tickets.append(push.Ticket(ok=False, gone=True, error="DeviceNotRegistered"))
            elif m["to"] in self.fail:
                tickets.append(push.Ticket(ok=False, error="MessageRateExceeded"))
            else:
                tickets.append(push.Ticket(ok=True))
        return tickets


async def test_register(client: httpx.AsyncClient) -> None:
    resp = await client.post("/devices/register", json={"expo_push_token": _TOKEN_A})
    assert resp.status_code == 200, resp.text
    first = resp.json()
    assert first["expo_push_token"] == _TOKEN_A and first["id"]

    await asyncio.sleep(0.01)
    again = (await client.post("/devices/register", json={"expo_push_token": _TOKEN_A})).json()
    assert again["id"] == first["id"], "registering a known token must not add a row"
    assert again["created_at"] == first["created_at"]
    assert again["last_registered_at"] > first["last_registered_at"]
    assert await _tokens() == [_TOKEN_A]

    legacy = await client.post("/devices/register", json={"expo_push_token": "ExpoPushToken[cccccccccccc]"})
    assert legacy.status_code == 200, "the ExpoPushToken[...] spelling is valid too"
    print("  [1] ✓ POST /devices/register stores a token once and only refreshes last_registered_at on repeats")


async def test_register_validation(client: httpx.AsyncClient) -> None:
    before = await _tokens()
    for bad in ("", "not-a-token", "ExponentPushToken[]", "ExponentPushToken[a b]",
                "ExponentPushToken[x]trailing", "ExponentPushToken[" + "x" * 300 + "]"):
        resp = await client.post("/devices/register", json={"expo_push_token": bad})
        assert resp.status_code == 422, (bad[:30], resp.status_code)
    assert (await client.post("/devices/register", json={})).status_code == 422
    assert await _tokens() == before
    print("  [2] ✓ malformed, empty, oversize or missing tokens are rejected (422) and nothing is stored")


async def test_notify_without_devices(client: httpx.AsyncClient) -> None:
    async with _Session() as db:
        await db.execute(models.DeviceToken.__table__.delete())
        await db.commit()
    expo = _Expo()
    original, push.send_expo_push = push.send_expo_push, expo
    try:
        resp = await client.post("/notify/test")
    finally:
        push.send_expo_push = original
    assert resp.status_code == 409, resp.text
    assert not expo.calls, "nothing is sent when no device is registered"
    print("  [3] ✓ POST /notify/test with no registered device answers 409 and sends nothing")


async def test_notify_delivery_and_cleanup(client: httpx.AsyncClient) -> None:
    for token in (_TOKEN_A, _TOKEN_B):
        await client.post("/devices/register", json={"expo_push_token": token})

    expo = _Expo()
    original, push.send_expo_push = push.send_expo_push, expo
    try:
        resp = await client.post("/notify/test")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"devices": 2, "sent": 2, "failed": 0, "removed": 0}, resp.json()
        sent = expo.calls[0]
        assert sorted(m["to"] for m in sent) == [_TOKEN_A, _TOKEN_B]
        assert sent[0]["title"] and sent[0]["body"] and sent[0]["data"] == {"type": "test"}
        assert sent[0]["channelId"] == "command-center" and sent[0]["priority"] == "high"

        # A transient failure keeps the token; DeviceNotRegistered deletes it.
        push.send_expo_push = _Expo(fail={_TOKEN_A}, gone={_TOKEN_B})
        resp = await client.post("/notify/test")
        assert resp.json() == {"devices": 2, "sent": 0, "failed": 2, "removed": 1}, resp.json()
        assert await _tokens() == [_TOKEN_A], await _tokens()
    finally:
        push.send_expo_push = original
    print("  [4] ✓ /notify/test reaches every device; a dead token is deleted, a transient failure is kept")


def _stub_expo(handler):
    """Make push.send_expo_push talk to *handler* instead of exp.host."""
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    httpx.AsyncClient = factory
    return real


async def test_expo_client() -> None:
    messages = [{"to": _TOKEN_A}, {"to": _TOKEN_B}, {"to": "ExponentPushToken[cccc]"}]
    seen = {}

    def ok_handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read()
        return httpx.Response(200, json={"data": [
            {"status": "ok", "id": "1"},
            {"status": "error", "message": "gone", "details": {"error": "DeviceNotRegistered"}},
            {"status": "error", "message": "slow down", "details": {"error": "MessageRateExceeded"}},
        ]})

    real = _stub_expo(ok_handler)
    try:
        tickets = await push.send_expo_push(messages)
    finally:
        httpx.AsyncClient = real
    assert seen["url"] == push.EXPO_PUSH_URL
    assert [t.ok for t in tickets] == [True, False, False]
    assert [t.gone for t in tickets] == [False, True, False], "only DeviceNotRegistered is `gone`"

    # Network errors, HTTP errors, junk and short replies fail every message, never `gone`.
    def boom(request): raise httpx.ConnectError("no route")
    for handler in (
        boom,
        lambda r: httpx.Response(503),
        lambda r: httpx.Response(200, text="<html>"),
        lambda r: httpx.Response(200, json={"errors": []}),
        lambda r: httpx.Response(200, json={"data": [{"status": "ok"}]}),
    ):
        real = _stub_expo(handler)
        try:
            tickets = await push.send_expo_push(messages)
        finally:
            httpx.AsyncClient = real
        assert len(tickets) == 3 and not any(t.ok or t.gone for t in tickets), tickets
    print("  [5] ✓ Expo client maps tickets in order; transport, HTTP and malformed replies fail without marking tokens gone")


async def test_batching() -> None:
    async with _Session() as db:
        await db.execute(models.DeviceToken.__table__.delete())
        db.add_all(models.DeviceToken(expo_push_token=f"ExponentPushToken[t{i:04d}]") for i in range(230))
        await db.commit()
        expo = _Expo()
        original, push.send_expo_push = push.send_expo_push, expo
        try:
            result = await push.push_to_all(db, "t", "b")
        finally:
            push.send_expo_push = original
    assert [len(c) for c in expo.calls] == [100, 100, 30], [len(c) for c in expo.calls]
    assert (result.devices, result.sent) == (230, 230)
    print("  [6] ✓ more than 100 devices are sent in batches of at most 100")


async def main() -> None:
    print("🚀 Running Phase 1.4 (push registration & delivery) Verification Suite...\n")
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with _Session() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_api_key] = lambda: None  # authentication: verify_security.py

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await test_register(client)
        await test_register_validation(client)
        await test_notify_without_devices(client)
        await test_notify_delivery_and_cleanup(client)
    await test_expo_client()
    await test_batching()
    await _engine.dispose()
    print("\n🎉 All push checks passed successfully!")


if __name__ == "__main__":
    asyncio.run(main())
