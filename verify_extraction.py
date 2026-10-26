"""
verify_extraction.py
Phase 0.5 — Extraction & dispatch verification suite.

With the LLM and Telegram fully mocked, covers:
  • the extractor: strict JSON-schema requests, ExtractionFailed for every hard
    failure (no regex fallback), and the JSON-object / no-format fallbacks
  • ExtractionFailed reaching the message ledger as FAILED, then retried to DONE
  • message headers [msg <id> · posted <IST> · edited] and the prompt rules
  • the dispatcher: updates by target_event_id, edits that update instead of
    duplicating, duplicate suppression, needs_review flags, source message ids,
    form replacement, note kinds, environment-scoped company names, per-action
    rollback
  • tracking-status invariants 1–9 (PROJECT_STATE §0.1) through the API

Every database is an in-memory SQLite instance; nothing touches the network.
Run: python verify_extraction.py
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import asyncio
import importlib.util
import json
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import openai
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# Patch DB engine BEFORE importing app modules (same pattern as Phase 2/4)
# ---------------------------------------------------------------------------
import app.database as _db_module

_test_engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
_session_maker = async_sessionmaker(
    bind=_test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class _TurnTakingSession:
    """Every session of an in-memory database shares one connection; here
    sessions take turns so concurrent tasks never interleave transactions."""

    _lock = None

    def __init__(self) -> None:
        self._session = None

    async def __aenter__(self) -> AsyncSession:
        if _TurnTakingSession._lock is None:
            _TurnTakingSession._lock = asyncio.Lock()
        await _TurnTakingSession._lock.acquire()
        self._session = _session_maker()
        return await self._session.__aenter__()

    async def __aexit__(self, *exc_info):
        try:
            return await self._session.__aexit__(*exc_info)
        finally:
            _TurnTakingSession._lock.release()


_TestSession = _TurnTakingSession
_db_module.engine = _test_engine
_db_module.AsyncSessionLocal = _TestSession
_db_module.install_sqlite_pragmas(_test_engine)  # enforce foreign keys like production

from alembic import command
from alembic.config import Config

from app import crud, worker
from app.ai import dispatcher
from app.ai import extractor
from app.ai.extractor import ExtractionFailed
from app.ai.prompts import build_system_prompt, format_message_block
from app.ai.schemas import RESULT_JSON_SCHEMA, ExtractedEvent, LLMExtractionResult
from app.config import Settings, settings
from app.models import Company, MessageLedger, NaiveDatetimeWarning, RoleTrack, SystemConfig, TimelineEvent
from app.sources import MessageSource
from app.telegram import client as tg_client_module
from app.telegram.listener import TelegramIngestionService

# Every datetime handed to the ORM must be timezone-aware.
warnings.simplefilter("error", NaiveDatetimeWarning)

ROOT = Path(__file__).resolve().parent


class _ForbiddenTelegramClient:
    def __init__(self, *args, **kwargs):
        raise AssertionError("A real TelegramClient was constructed during the tests")


tg_client_module.TelegramClient = _ForbiddenTelegramClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


_REQUEST = httpx.Request("POST", "https://llm.invalid/v1/chat/completions")


def _bad_request(code: str) -> openai.BadRequestError:
    return openai.BadRequestError(
        f"Error code: 400 - {code}", response=httpx.Response(400, request=_REQUEST), body={"error": {"code": code}}
    )


_FULL_ACTION = {
    "action": "CREATE_EVENT", "company_name": "Acme", "role_name": "SDE", "drive_type": "FTE",
    "audience_scope": "COHORT", "event_type": "OA_WINDOW", "title": "Acme OA", "action_link": None,
    "start_time": "2026-01-02T17:00:00+05:30", "end_time": "2026-01-02T19:00:00+05:30",
    "status_note": None, "note_kind": None, "target_event_id": None, "source_message_id": 12,
    "confidence": "HIGH",
}
VALID_JSON = json.dumps({"company_name": "Acme", "actions": [_FULL_ACTION]})


class FakeLLM:
    """Stands in for the OpenAI client: replays scripted answers or errors and
    records every request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        content, finish = answer if isinstance(answer, tuple) else (answer, "stop")
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content))])

    @property
    def formats(self):
        return [(r.get("response_format") or {}).get("type") for r in self.requests]


class FakeExtractor:
    """Stands in for extract_placement_actions: always returns *result* and
    records the text block and context of each call."""

    def __init__(self, result=None):
        self.result = result or LLMExtractionResult()
        self.calls = []

    async def __call__(self, raw_text, current_context=None):
        self.calls.append((raw_text, current_context or {}))
        return self.result.model_copy(deep=True)


def _action(action="CREATE_EVENT", **fields) -> ExtractedEvent:
    fields.setdefault("audience_scope", "COHORT")
    fields.setdefault("confidence", "HIGH")
    if action in ("CREATE_EVENT", "EXTEND_DEADLINE", "RESCHEDULE_EVENT", "REPLACE_FORM"):
        fields.setdefault("event_type", "OA_WINDOW")
    return ExtractedEvent(action=action, **fields)


def _result(*actions, company_name=None) -> LLMExtractionResult:
    return LLMExtractionResult(company_name=company_name, actions=list(actions))


def _message(message_id, text="Update", *, posted=None, edited=False) -> dict:
    return {"id": message_id, "text": text, "date": (posted or _now()).timestamp(), "is_edit": edited}


async def _dispatch(result, *, chat_id, context="subchannel", messages, **kwargs) -> FakeExtractor:
    fake = result if isinstance(result, FakeExtractor) else FakeExtractor(result)
    with patch("app.ai.extractor.extract_placement_actions", fake):
        async with _TestSession() as db:
            await dispatcher.process_and_dispatch(
                chat_id=chat_id, context_header=context, messages=messages, db=db, **kwargs
            )
    return fake


async def _company(name, *, chat_id=None, env="MAIN", mode="ELIGIBLE", roles=(("SDE", "ELIGIBLE", True),), active=True) -> int:
    async with _TestSession() as db:
        company = Company(
            name=name, normalized_name=name.lower(), environment_type=env,
            subchannel_id=chat_id, tracking_mode=mode, is_active=active,
        )
        db.add(company)
        await db.flush()
        for role_name, role_mode, triaged in roles:
            db.add(RoleTrack(company_id=company.id, role_name=role_name, tracking_mode=role_mode, is_triaged=triaged))
        await db.commit()
        return company.id


async def _event(company_id, event_type, end, *, start=None, title=None, version=1, source=None) -> int:
    async with _TestSession() as db:
        role_id = (await db.execute(
            select(RoleTrack.id).where(RoleTrack.company_id == company_id).order_by(RoleTrack.id)
        )).scalars().first()
        event = TimelineEvent(
            company_id=company_id, role_track_id=role_id, event_type=event_type,
            title=title or event_type.title(), start_time=start, end_time=end, version=version,
            source_chat_id=source[0] if source else None, source_message_id=source[1] if source else None,
        )
        db.add(event)
        await db.commit()
        return event.id


async def _events(company_id, *, include_deleted=False):
    async with _TestSession() as db:
        query = select(TimelineEvent).where(TimelineEvent.company_id == company_id).order_by(TimelineEvent.id)
        if not include_deleted:
            query = query.where(TimelineEvent.is_deleted == False)  # noqa: E712
        return list((await db.execute(query)).scalars().all())


async def _get(model, key):
    async with _TestSession() as db:
        return await db.get(model, key)


async def _roles(company_id):
    async with _TestSession() as db:
        result = await db.execute(select(RoleTrack).where(RoleTrack.company_id == company_id).order_by(RoleTrack.id))
        return list(result.scalars().all())


async def _companies_named(name, env):
    async with _TestSession() as db:
        result = await db.execute(
            select(Company).where(Company.normalized_name == name.lower(), Company.environment_type == env)
        )
        return list(result.scalars().all())


def _close(a: datetime, b: datetime) -> bool:
    return abs((a - b).total_seconds()) < 2


def _check_strict(schema, path="$") -> None:
    """Groq strict mode: every object closed and every property required."""
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False, path
            assert sorted(schema.get("required", [])) == sorted(schema["properties"]), path
        for key, value in schema.items():
            _check_strict(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for index, item in enumerate(schema):
            _check_strict(item, f"{path}[{index}]")


def _migrate(connection) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["connection"] = connection
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

async def test_strict_schema_request() -> None:
    extractor._strict_mode = True
    fake = FakeLLM(VALID_JSON, VALID_JSON)
    with patch.object(extractor, "get_ai_client", return_value=fake):
        result = await extractor.extract_placement_actions("[msg 12 · posted …]\nAcme OA on Friday", {})
        original = settings.LLM_REASONING_EFFORT
        settings.LLM_REASONING_EFFORT = "low"
        try:
            await extractor.extract_placement_actions("text", {})
        finally:
            settings.LLM_REASONING_EFFORT = original

    request = fake.requests[0]
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["response_format"]["json_schema"]["schema"] is RESULT_JSON_SCHEMA
    assert request["temperature"] == 0.0 and request["max_completion_tokens"] == 8192
    assert "reasoning_effort" not in request  # provider default unless configured
    assert fake.requests[1]["reasoning_effort"] == "low"
    assert result.actions[0].source_message_id == 12 and result.actions[0].confidence == "HIGH"

    _check_strict(RESULT_JSON_SCHEMA)
    assert set(RESULT_JSON_SCHEMA["properties"]) == set(LLMExtractionResult.model_fields)
    assert set(RESULT_JSON_SCHEMA["properties"]["actions"]["items"]["properties"]) == set(ExtractedEvent.model_fields)
    assert importlib.util.find_spec("app.ai.rule_extractor") is None, "the regex fallback must be gone"
    try:
        Settings(_env_file=None, LLM_REASONING_EFFORT="extreme")
    except ValidationError:
        pass
    else:
        raise AssertionError("an unknown LLM_REASONING_EFFORT was accepted")

    print("  [1] ✓ Extractor requests Groq strict JSON-schema output (schema closed, all fields required, in step with the model); regex fallback module removed")


async def test_hard_failures_raise() -> None:
    extractor._strict_mode = True
    secret = "Ravi 9876543210"
    fake = FakeLLM('{"company_name": null, "actions": []}')
    with patch.object(extractor, "get_ai_client", return_value=fake):
        empty = await extractor.extract_placement_actions(secret, {})
    assert empty.actions == []  # a valid "nothing to do" answer is not a failure

    failures = [
        ("prose instead of JSON", "I could not find anything", "does not match the schema"),
        ("unknown action", '{"company_name": null, "actions": [{"action": "TELEPORT"}]}', "does not match the schema"),
        ("empty answer", "", "empty response"),
        ("cut off", ('{"company_name": "Ac', "length"), "cut off"),
        ("timeout", openai.APITimeoutError(request=_REQUEST), "APITimeoutError"),
        ("network", openai.APIConnectionError(request=_REQUEST), "APIConnectionError"),
        ("rate limit", openai.RateLimitError("429", response=httpx.Response(429, request=_REQUEST), body=None), "RateLimitError"),
    ]
    for label, answer, fragment in failures:
        fake = FakeLLM(answer)
        with patch.object(extractor, "get_ai_client", return_value=fake):
            try:
                await extractor.extract_placement_actions(secret, {})
            except ExtractionFailed as exc:
                assert fragment in str(exc), (label, str(exc))
                assert "9876543210" not in str(exc), label  # message text never in the stored error
            else:
                raise AssertionError(f"{label}: no ExtractionFailed")

    print("  [2] ✓ Hard failures (invalid/empty/truncated output, timeouts, network, rate limits) raise ExtractionFailed; a valid empty answer does not")


async def test_output_mode_fallbacks() -> None:
    try:
        extractor._strict_mode = True
        fake = FakeLLM(_bad_request("invalid_schema"), VALID_JSON, VALID_JSON)
        with patch.object(extractor, "get_ai_client", return_value=fake):
            await extractor.extract_placement_actions("text", {})
            await extractor.extract_placement_actions("text", {})
        assert fake.formats == ["json_schema", "json_object", "json_object"], fake.formats
        assert extractor._strict_mode is False  # off for the rest of the process

        fake = FakeLLM(_bad_request("json_validate_failed"), "```json\n" + VALID_JSON + "\n```")
        with patch.object(extractor, "get_ai_client", return_value=fake):
            result = await extractor.extract_placement_actions("text", {})
        assert fake.formats == ["json_object", None] and result.company_name == "Acme"

        fake = FakeLLM(_bad_request("context_length_exceeded"))
        with patch.object(extractor, "get_ai_client", return_value=fake):
            try:
                await extractor.extract_placement_actions("text", {})
            except ExtractionFailed as exc:
                assert "rejected" in str(exc)
            else:
                raise AssertionError("a rejected request did not raise ExtractionFailed")

        extractor._strict_mode = True
        fake = FakeLLM(_bad_request("invalid_schema"), _bad_request("context_length_exceeded"))
        with patch.object(extractor, "get_ai_client", return_value=fake):
            try:
                await extractor.extract_placement_actions("text", {})
            except ExtractionFailed:
                pass
            else:
                raise AssertionError("expected ExtractionFailed")
        assert extractor._strict_mode is True  # JSON-object mode failed too: strict mode is kept
    finally:
        extractor._strict_mode = True

    print("  [3] ✓ Fallbacks: rejected strict mode → JSON-object mode (then off for the process); json_validate_failed → no-format retry; other rejections → ExtractionFailed")


async def test_failed_extraction_is_retried() -> None:
    chat = -1003100000001
    company_id = await _company("Retry Co", chat_id=chat)
    service = TelegramIngestionService(SimpleNamespace(), {"MAIN"})
    service.active_subchannel_ids.add(chat)
    await service.set_payload_handler(worker.dispatch_to_ai)
    message = {"id": 70, "text": "Retry Co OA on Friday 5 PM", "raw_text": "Retry Co OA on Friday 5 PM",
               "content_hash": "h", "date": _now().timestamp(), "is_edit": False}

    failing = AsyncMock(side_effect=ExtractionFailed("LLM request failed (APITimeoutError)"))
    with patch("app.ai.extractor.extract_placement_actions", failing):
        assert await service._emit(MessageSource(chat, "subchannel"), [message]) is False
    async with _TestSession() as db:
        row = await db.get(MessageLedger, (chat, 70))
    assert row.status == "FAILED" and row.attempts == 1
    assert row.reason == "ExtractionFailed: LLM request failed (APITimeoutError)", row.reason
    assert await _events(company_id) == []  # nothing guessed in place of the LLM

    with patch("app.ai.extractor.extract_placement_actions", FakeExtractor(_result(_action(end_time=_now() + timedelta(days=3))))):
        assert await service._emit(MessageSource(chat, "subchannel"), [message]) is True
    async with _TestSession() as db:
        row = await db.get(MessageLedger, (chat, 70))
    assert row.status == "DONE"
    events = await _events(company_id)
    assert len(events) == 1 and (events[0].source_chat_id, events[0].source_message_id) == (chat, 70)

    print("  [4] ✓ ExtractionFailed → ledger FAILED (reason without message text), nothing guessed; the retry extracts and records DONE")


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

async def test_message_headers_and_prompt_rules() -> None:
    posted = datetime(2026, 1, 1, 8, 35, tzinfo=timezone.utc)  # Thursday 14:05 IST
    block = format_message_block([
        {"id": 12, "text": "OA tomorrow 5 PM", "date": posted.timestamp(), "is_edit": True},
        {"id": 13, "text": "   ", "date": posted.timestamp()},
        {"id": 14, "text": "[CONTEXT: Acme drive]\nForm closes tonight", "date": posted.timestamp(), "is_edit": False},
    ])
    assert block == (
        "[msg 12 · posted Thu 01 Jan 2026, 14:05 IST · edited]\nOA tomorrow 5 PM\n\n"
        "[msg 14 · posted Thu 01 Jan 2026, 14:05 IST]\n[CONTEXT: Acme drive]\nForm closes tonight"
    ), block

    context = {
        "company_id": 7,
        "companies": [{
            "company_id": 7, "company_name": "Acme", "roles": ["SDE"],
            "events": [{
                "id": 41, "title": "Acme OA", "event_type": "OA_WINDOW", "role_name": "SDE",
                "start_time": None, "end_time": "2026-01-02T17:00:00+05:30", "action_link": None,
                "user_status": "PENDING", "source_message_id": 12,
            }],
        }],
    }
    prompt = build_system_prompt(context)
    for phrase in (
        "id=41", "from msg 12", "Roles: SDE", "every message below is about this company",
        "POSTED time in the header", "target_event_id", "source_message_id", "CRITERIA_RELAXATION",
        "CONFIDENCE", '"edited"', "A reminder or repeat of a scheduled event with the same timing",
    ):
        assert phrase in prompt, phrase
    # The old rules that turned reminders into new events are gone.
    assert 'ONLY use action="EXTEND_DEADLINE"' not in prompt
    assert "Use action=\"CREATE_EVENT\" whenever" not in prompt
    assert "No tracked company is known" in build_system_prompt({})

    print("  [5] ✓ Messages carry [msg <id> · posted <IST> · edited] headers; the prompt lists event ids and sources and resolves dates against post times")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def test_updates_target_events_by_id() -> None:
    chat = -1003200000001
    company_id = await _company("Target Co", chat_id=chat)
    first_end = _now() + timedelta(days=2)
    first = await _event(company_id, "IRIS_DEADLINE", first_end, start=first_end)  # point deadline
    second = await _event(company_id, "IRIS_DEADLINE", _now() + timedelta(days=5))

    new_end = _now() + timedelta(days=4)
    fake = await _dispatch(
        _result(_action("EXTEND_DEADLINE", event_type="IRIS_DEADLINE", target_event_id=first, end_time=new_end)),
        chat_id=chat, messages=[_message(80, "IRIS deadline extended")],
    )
    assert [e["id"] for e in fake.calls[0][1]["events"]] == [first, second]  # ids shown to the LLM
    extended = await _get(TimelineEvent, first)
    assert _close(extended.end_time, new_end) and _close(extended.start_time, new_end)  # point deadline moved whole
    assert extended.needs_review is False and _close((await _get(TimelineEvent, second)).end_time, _now() + timedelta(days=5))

    later = _now() + timedelta(days=6)
    await _dispatch(
        _result(_action("EXTEND_DEADLINE", event_type="IRIS_DEADLINE", target_event_id=99999, end_time=later)),
        chat_id=chat, messages=[_message(81)],
    )
    flagged = await _get(TimelineEvent, second)  # latest of the two IRIS events
    assert _close(flagged.end_time, later) and flagged.needs_review is True
    assert "target event 99999 not found" in flagged.review_reason
    assert "several matching events" in flagged.review_reason

    print("  [6] ✓ Updates use target_event_id (point deadlines move whole); an unknown id falls back to the type match and is flagged for review")


async def test_edits_update_instead_of_duplicating() -> None:
    chat = -1003300000001
    company_id = await _company("Edit Co", chat_id=chat)
    friday = _now() + timedelta(days=3)
    await _dispatch(_result(_action(end_time=friday, source_message_id=501)), chat_id=chat, messages=[_message(501, "OA Friday 5 PM")])
    [created] = await _events(company_id)
    assert (created.source_chat_id, created.source_message_id) == (chat, 501)

    # The coordinator edits message 501; the LLM answers CREATE_EVENT again without a target.
    moved = friday + timedelta(hours=1)
    fake = await _dispatch(
        _result(_action(end_time=moved, source_message_id=501)),
        chat_id=chat, messages=[_message(501, "OA Friday 6 PM", edited=True)],
    )
    assert fake.calls[0][1]["events"][0]["source_message_id"] == 501  # shown as "from msg 501"
    events = await _events(company_id)
    assert len(events) == 1 and events[0].id == created.id and _close(events[0].end_time, moved)

    # A repeat of the same event from another message is not created twice.
    await _dispatch(_result(_action(end_time=moved)), chat_id=chat, messages=[_message(502, "Reminder: OA Friday 6 PM")])
    assert len(await _events(company_id)) == 1

    print("  [7] ✓ An edited message updates the event it created (no duplicate); a repeated announcement of the same event is not created twice")


async def test_review_flags() -> None:
    chat = -1003400000001
    company_id = await _company("Review Co", chat_id=chat)
    iris = await _event(company_id, "IRIS_DEADLINE", _now() + timedelta(days=5))
    posted = _now()
    await _dispatch(
        _result(
            _action(event_type="PPT", title="Guessed PPT", end_time=posted + timedelta(days=1), confidence="LOW"),
            _action(event_type="INTERVIEW", title="Past interview", end_time=posted - timedelta(days=2)),
            _action(event_type="CUSTOM", title="Far event", end_time=posted + timedelta(days=400)),
            _action("EXTEND_DEADLINE", event_type="IRIS_DEADLINE", target_event_id=iris, end_time=posted + timedelta(days=2)),
            _action(event_type="FORM_SUBMISSION", title="Clean form", end_time=posted + timedelta(days=2)),
        ),
        chat_id=chat, messages=[_message(90, "Several updates", posted=posted)],
    )
    by_title = {e.title: e for e in await _events(company_id)}
    expectations = {
        "Guessed PPT": "low confidence",
        "Past interview": "date is before the announcement",
        "Far event": "date is more than a year away",
        "Iris_Deadline": "the extension moves the deadline earlier",
    }
    for title, reason in expectations.items():
        assert by_title[title].needs_review and reason in by_title[title].review_reason, (title, by_title[title].review_reason)
    assert by_title["Clean form"].needs_review is False and by_title["Clean form"].review_reason is None

    print("  [8] ✓ Uncertain results are kept but flagged needs_review: low confidence, date before the post, > 1 year away, an extension that moves earlier")


async def test_source_ids_and_form_replacement() -> None:
    chat = -1003500000001
    company_id = await _company("Source Co", chat_id=chat)
    await _dispatch(
        _result(
            _action(event_type="PPT", title="From 602", end_time=_now() + timedelta(days=1), source_message_id=602),
            _action(event_type="OA_WINDOW", title="Unknown source", end_time=_now() + timedelta(days=2), source_message_id=999),
        ),
        chat_id=chat, messages=[_message(601, "First"), _message(602, "Second")],
    )
    by_title = {e.title: e for e in await _events(company_id)}
    assert (by_title["From 602"].source_chat_id, by_title["From 602"].source_message_id) == (chat, 602)
    assert by_title["Unknown source"].source_message_id is None  # several messages: not guessed

    old_form = await _event(company_id, "FORM_SUBMISSION", _now() + timedelta(days=1), title="Form v1")
    await _dispatch(
        _result(_action("REPLACE_FORM", event_type="FORM_SUBMISSION", target_event_id=old_form,
                        action_link="https://forms.gle/new", end_time=_now() + timedelta(days=3))),
        chat_id=chat, messages=[_message(603, "Fill the new form instead")],
    )
    assert (await _get(TimelineEvent, old_form)).is_deleted is True  # the superseded form is hidden
    forms = [e for e in await _events(company_id) if e.event_type == "FORM_SUBMISSION"]
    assert len(forms) == 1 and forms[0].action_link == "https://forms.gle/new" and forms[0].version == 2
    assert forms[0].source_message_id == 603

    print("  [9] ✓ Events record their source message (never guessed in a multi-message batch); REPLACE_FORM hides the form it supersedes")


async def test_note_kinds_and_ignored_roles() -> None:
    chat = -1003600000001
    company_id = await _company(
        "Note Co", chat_id=chat, mode="SILENT_TRACK",
        roles=(("SDE", "SILENT_TRACK", True), ("Analyst", "SILENT_TRACK", True), ("PM", "IGNORED", True)),
    )
    general = _action("STATUS_NOTE", status_note="Results next week", note_kind="GENERAL")
    await _dispatch(_result(general), chat_id=chat, messages=[_message(110)])
    sde, analyst, pm = await _roles(company_id)
    assert sde.latest_status_note == analyst.latest_status_note == "Results next week"
    assert sde.is_triaged and analyst.is_triaged  # a GENERAL note never re-opens Triage

    relaxed = _action("STATUS_NOTE", status_note="Now open to 6.5 CGPA", note_kind="CRITERIA_RELAXATION")
    await _dispatch(_result(relaxed), chat_id=chat, messages=[_message(111)])
    sde, analyst, pm = await _roles(company_id)
    assert not sde.is_triaged and not analyst.is_triaged  # re-opened as a proposal
    assert sde.tracking_mode == analyst.tracking_mode == "SILENT_TRACK"  # never auto-promoted
    assert sde.latest_note_kind == "CRITERIA_RELAXATION"
    assert pm.latest_status_note is None and pm.tracking_mode == "IGNORED" and pm.is_triaged

    await _dispatch(
        _result(_action("STATUS_NOTE", role_name="PM", status_note="PM open to all", note_kind="CRITERIA_RELAXATION"),
                _action(role_name="PM", event_type="INTERVIEW", end_time=_now() + timedelta(days=2))),
        chat_id=chat, messages=[_message(112)],
    )
    assert (await _roles(company_id))[2].latest_status_note is None
    assert await _events(company_id) == []  # nothing for the IGNORED role

    eligible_chat = -1003600000002
    eligible_id = await _company("Eligible Note Co", chat_id=eligible_chat)
    await _dispatch(_result(relaxed), chat_id=eligible_chat, messages=[_message(113)])
    [role] = await _roles(eligible_id)
    assert role.latest_status_note == "Now open to 6.5 CGPA" and role.is_triaged  # ELIGIBLE: noted, not re-opened

    print("  [10] ✓ note_kind: only CRITERIA_RELAXATION re-opens SILENT_TRACK roles (mode unchanged); ELIGIBLE noted silently; IGNORED roles never touched")


async def test_root_feed_company_names() -> None:
    feed = -1003700000001
    acme_id = await _company("Acme Root")
    await _event(acme_id, "OA_WINDOW", _now() + timedelta(days=2), title="Acme Root OA")
    gone_id = await _company("Gone Co", active=False)

    # A TEST-feed post never touches a MAIN company of the same name.
    await _dispatch(
        _result(_action(company_name="Acme Root", event_type="PPT", end_time=_now() + timedelta(days=1))),
        chat_id=feed, context="test_feed", messages=[_message(120, "Acme Root PPT tomorrow")],
    )
    [test_acme] = await _companies_named("Acme Root", "TEST")
    assert test_acme.tracking_mode == "SILENT_TRACK" and not (await _roles(test_acme.id))[0].is_triaged
    assert [e.title for e in await _events(acme_id)] == ["Acme Root OA"]

    # A deleted company's name: nothing applied, nothing re-created.
    await _dispatch(
        _result(_action(company_name="Gone Co", end_time=_now() + timedelta(days=1))),
        chat_id=feed, context="main_feed", messages=[_message(121, "Gone Co OA")],
    )
    assert len(await _companies_named("Gone Co", "MAIN")) == 1 and await _events(gone_id) == []

    # A post naming a tracked company gets that company's schedule (with ids).
    fake = await _dispatch(
        _result(_action("NO_ACTION")),
        chat_id=feed, context="main_feed", messages=[_message(122, "Reminder: Acme Root OA closes Friday")],
    )
    companies = fake.calls[0][1]["companies"]
    assert [c["company_name"] for c in companies] == ["Acme Root"] and companies[0]["events"][0]["title"] == "Acme Root OA"

    # A digest post: each action goes to its own company.
    await _dispatch(
        _result(
            _action(company_name="Acme Root", event_type="INTERVIEW", title="Acme interviews", end_time=_now() + timedelta(days=4)),
            _action(company_name="Beta Root", event_type="PPT", title="Beta PPT", end_time=_now() + timedelta(days=4)),
        ),
        chat_id=feed, context="main_feed", messages=[_message(123, "Acme Root interviews; Beta Root PPT")],
    )
    [beta] = await _companies_named("Beta Root", "MAIN")
    assert "Acme interviews" in [e.title for e in await _events(acme_id)]
    assert [e.title for e in await _events(beta.id)] == ["Beta PPT"]

    print("  [11] ✓ Root-feed posts: names matched within the environment, deleted companies left alone, mentioned schedules shown, digest actions split by company")


async def test_failed_action_is_rolled_back_alone() -> None:
    chat = -1003800000001
    company_id = await _company("Rollback Co", chat_id=chat)
    original = crud.create_custom_event
    calls = {"count": 0}

    async def flaky(db, event_in):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("database hiccup")
        return await original(db, event_in)

    with patch.object(crud, "create_custom_event", flaky):
        await _dispatch(
            _result(_action(event_type="PPT", title="Lost", end_time=_now() + timedelta(days=1)),
                    _action(event_type="INTERVIEW", title="Kept", end_time=_now() + timedelta(days=2))),
            chat_id=chat, messages=[_message(130)],
        )
    assert [e.title for e in await _events(company_id)] == ["Kept"]

    print("  [12] ✓ A failed action is rolled back without affecting the rest of the batch")


# ---------------------------------------------------------------------------
# Invariants 1–9 through the API
# ---------------------------------------------------------------------------

async def test_tracking_invariants() -> None:
    from httpx import ASGITransport, AsyncClient

    from app.database import get_db
    from app.main import app

    async def override_get_db():
        async with _TestSession() as session:
            yield session

    ignored_id = await _company("Inv Ignored Co", mode="IGNORED", roles=(("SDE", "IGNORED", True),))
    locked_id = await _company("Inv Locked Co", mode="IGNORED", roles=(("SDE", "SILENT_TRACK", False),))
    mixed_id = await _company("Inv Mixed Co", mode="SILENT_TRACK", roles=(("A", "SILENT_TRACK", False), ("B", "SILENT_TRACK", False)))
    silent_chat = -1003900000001
    silent_id = await _company("Inv Silent Co", chat_id=silent_chat, mode="SILENT_TRACK", roles=(("C", "SILENT_TRACK", True),))
    [ignored_role] = await _roles(ignored_id)
    [locked_role] = await _roles(locked_id)
    role_a, role_b = await _roles(mixed_id)
    await _event(locked_id, "OA_WINDOW", _now() + timedelta(days=1), title="Locked OA")
    await _dispatch(_result(_action(title="Silent OA", end_time=_now() + timedelta(days=2))), chat_id=silent_chat, messages=[_message(140)])

    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 5 + 1: triage never changes an IGNORED role or a role of an IGNORED company.
            for path, body in ((f"/triage/{ignored_role.id}/accept", None), (f"/triage/{locked_role.id}", {"action": "ELIGIBLE"})):
                response = await client.post(path, json=body)
                assert response.status_code == 409 and "Companies screen" in response.json()["detail"], (path, response.text)
            pending = [item["id"] for item in (await client.get("/triage/pending")).json()]
            assert locked_role.id not in pending and ignored_role.id not in pending
            response = await client.post(
                f"/companies/{ignored_id}/tracks", json={"company_id": ignored_id, "role_name": "New role"}
            )
            assert response.status_code == 409  # a new role would bring an IGNORED company back

            # 2: discarding the last tracked role makes the company IGNORED.
            assert (await client.post(f"/triage/{role_a.id}/ignore")).status_code == 200
            assert (await _get(Company, mixed_id)).tracking_mode == "SILENT_TRACK"
            assert (await client.post(f"/triage/{role_b.id}/ignore")).status_code == 200
            assert (await _get(Company, mixed_id)).tracking_mode == "IGNORED"

            # 8 + 3: SILENT_TRACK events and IGNORED companies stay off the timeline.
            titles = [e["title"] for e in (await client.get("/events/timeline")).json()]
            assert "Silent OA" not in titles and "Locked OA" not in titles

            # 1 + 9: only the Companies-screen toggle brings a company back; it triages every role.
            response = await client.patch(f"/companies/{mixed_id}/tracking", json={"tracking_mode": "ELIGIBLE"})
            assert response.status_code == 200
            assert all(r.tracking_mode == "ELIGIBLE" and r.is_triaged for r in await _roles(mixed_id))
            response = await client.patch(f"/companies/{silent_id}/tracking", json={"tracking_mode": "ELIGIBLE"})
            assert response.status_code == 200
            titles = [e["title"] for e in (await client.get("/events/timeline")).json()]
            assert "Silent OA" in titles
            payload = (await client.get("/events/timeline")).json()
            silent_event = next(e for e in payload if e["title"] == "Silent OA")
            assert silent_event["source_message_id"] == 140 and silent_event["needs_review"] is False
    finally:
        app.dependency_overrides.clear()

    # 7: no CGPA/branch exists, and the profile never reaches the LLM.
    assert not [c for c in SystemConfig.__table__.columns.keys() if "cgpa" in c or "branch" in c]
    async with _TestSession() as db:
        config = (await db.execute(select(SystemConfig))).scalars().first() or SystemConfig()
        config.student_name, config.roll_number = "Profile Name", "211CS999"
        db.add(config)
        await db.commit()
    extractor._strict_mode = True
    fake_llm = FakeLLM('{"company_name": null, "actions": []}')
    with patch.object(extractor, "get_ai_client", return_value=fake_llm):
        async with _TestSession() as db:
            await dispatcher.process_and_dispatch(
                chat_id=silent_chat, context_header="subchannel", messages=[_message(141, "Shortlist out")], db=db
            )
    sent = json.dumps(fake_llm.requests[0]["messages"])
    assert "211CS999" not in sent and "Profile Name" not in sent

    print("  [13] ✓ Invariants: 409 on IGNORED roles/companies (triage, new roles); last discard ignores the company; SILENT_TRACK/IGNORED off the timeline; PATCH is the only way back and triages every role; no profile data to the LLM")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def main() -> None:
    async with _test_engine.begin() as conn:
        await conn.run_sync(_migrate)

    print("\nRunning Phase 0.5 extraction & dispatch verification suite…\n")

    await test_strict_schema_request()
    await test_hard_failures_raise()
    await test_output_mode_fallbacks()
    await test_failed_extraction_is_retried()
    await test_message_headers_and_prompt_rules()
    await test_updates_target_events_by_id()
    await test_edits_update_instead_of_duplicating()
    await test_review_flags()
    await test_source_ids_and_form_replacement()
    await test_note_kinds_and_ignored_roles()
    await test_root_feed_company_names()
    await test_failed_action_is_rolled_back_alone()
    await test_tracking_invariants()

    await _test_engine.dispose()
    print("\n✅ Phase 0.5 Extraction & Dispatch Verified Successfully!")


if __name__ == "__main__":
    asyncio.run(main())
