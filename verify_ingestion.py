"""
verify_ingestion.py
Phase 0.4 — Ingestion fixes verification suite.

With Telegram fully mocked, covers:
  • the join policy: which links are followed and which chats may be joined
  • the pre-filter: only messages without visible text are skipped
  • reactions: never subscribed to or read; edit updates that leave the text
    unchanged are ignored
  • invite posts: joined under the policy and extracted with their company;
    re-posts and replies attributed deterministically; IGNORED companies skipped
  • flood waits retried, joins paced
  • supergroups: only admins' posts; root feeds must be broadcast channels
  • debouncer max-wait and per-chat ordering; at most 40 messages per LLM call
  • catch-up replays edits made while offline
  • dispatcher: authoritative source company, subchannel naming, IGNORED gate

Every database is an in-memory SQLite instance and nothing touches the network:
the real TelegramClient class is replaced by one that fails if constructed, and
mocked entities stand in for every channel, person and bot — no live link is
ever resolved.
Run: python verify_ingestion.py
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import asyncio
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select, update
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
    """Every session of an in-memory database shares one connection, so the
    concurrent tasks of these tests would interleave their transactions on it —
    one task's rollback-on-close discarding another's pending writes. In
    production each session has its own connection; here sessions take turns."""

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
from telethon import errors, events, utils
from telethon.tl import functions, types

from app import crud, worker
from app.ai import dispatcher
from app.ai.schemas import ExtractedEvent, LLMExtractionResult
from app.config import settings
from app.models import (
    Company,
    MessageLedger,
    NaiveDatetimeWarning,
    RoleTrack,
    SystemConfig,
    TimelineEvent,
)
from app.sources import SubchannelLink
from app.telegram import client as tg_client_module
from app.telegram import join_policy, ledger
from app.telegram import listener as listener_module
from app.telegram.debouncer import ChannelDebouncer
from app.telegram.listener import TelegramIngestionService, _build_message_dict, content_hash

# Every datetime handed to the ORM must be timezone-aware.
warnings.simplefilter("error", NaiveDatetimeWarning)

ROOT = Path(__file__).resolve().parent
MAIN_FEED = -1001000000001  # the peer id of _channel(1000000001, …)


class _ForbiddenTelegramClient:
    """Installed in place of telethon's TelegramClient for the whole suite."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("A real TelegramClient was constructed during the tests")


tg_client_module.TelegramClient = _ForbiddenTelegramClient
settings.DEBOUNCE_DELAY_SECONDS = 0.05
settings.DEBOUNCE_MAX_WAIT_SECONDS = 1.0
settings.MAIN_FEED_CHANNEL_ID = None  # feeds come only from the test database
settings.TEST_FEED_CHANNEL_ID = None
listener_module._CHAT_PACING_SECONDS = 0
listener_module._JOIN_INTERVAL_SECONDS = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _channel(raw_id: int, title: str, *, megagroup: bool = False, username=None) -> types.Channel:
    """A broadcast channel (default) or a supergroup this account has not joined."""
    return types.Channel(
        id=raw_id,
        title=title,
        photo=types.ChatPhotoEmpty(),
        date=_now(),
        broadcast=None if megagroup else True,
        megagroup=True if megagroup else None,
        left=True,
        access_hash=raw_id,
        username=username,
    )


def _peer(chat) -> int:
    return utils.get_peer_id(chat)


def _msg(message_id: int, text, *, age=timedelta(minutes=5), edit_age=None, post=True,
         sender=None, reply_to=None, cls=SimpleNamespace):
    """Lightweight Telethon Message stand-in (a channel post unless post=False)."""
    return cls(
        id=message_id,
        text=text,
        date=_now() - age,
        edit_date=None if edit_age is None else _now() - edit_age,
        post=post,
        from_id=sender,
        reply_to_msg_id=reply_to,
        media=None,
    )


def _event(chat_id: int, message, chat=None) -> SimpleNamespace:
    return SimpleNamespace(chat_id=chat_id, message=message, chat=chat, is_private=False)


class FakeTelegram:
    """Stand-in for TelegramClient: serves fixed chats, invites and usernames,
    and records every request it receives. Nothing touches the network."""

    def __init__(self):
        self.requests = []  # (loop time, request), in order
        self.invites = {}  # hash -> ChatInvite preview, or an exception to raise
        self.invite_channels = {}  # hash -> the Channel that importing the invite joins
        self.joined = set()  # hashes imported so far
        self.import_errors = []  # raised by the next ImportChatInvite calls, in order
        self.usernames = {}  # username -> User / Channel
        self.history = {}  # chat_id -> messages
        self.entities = {}  # chat_id -> entity in the "session cache"
        self.admins = {}  # supergroup chat_id -> admin user ids, or an exception
        self.participant_calls = 0
        self.iter_calls = []
        self.handlers = []

    def add_invite(self, invite_hash: str, channel: types.Channel, **preview) -> None:
        self.invite_channels[invite_hash] = channel
        self.history.setdefault(_peer(channel), [])
        self.invites[invite_hash] = types.ChatInvite(
            title=channel.title,
            photo=types.PhotoEmpty(id=0),
            participants_count=100,
            color=0,
            channel=True,
            broadcast=channel.broadcast,
            megagroup=channel.megagroup,
            **preview,
        )

    @property
    def request_names(self):
        return [type(request).__name__ for _, request in self.requests]

    async def __call__(self, request):
        self.requests.append((asyncio.get_running_loop().time(), request))
        if isinstance(request, functions.messages.CheckChatInviteRequest):
            if request.hash in self.joined:
                return types.ChatInviteAlready(chat=self.invite_channels[request.hash])
            preview = self.invites[request.hash]
            if isinstance(preview, Exception):
                raise preview
            return preview
        if isinstance(request, functions.messages.ImportChatInviteRequest):
            if self.import_errors:
                raise self.import_errors.pop(0)
            channel = self.invite_channels[request.hash]
            self.joined.add(request.hash)
            self.entities[_peer(channel)] = channel
            return types.Updates(updates=[], users=[], chats=[channel], date=_now(), seq=0)
        if isinstance(request, functions.contacts.ResolveUsernameRequest):
            entity = self.usernames[request.username]
            if isinstance(entity, types.User):
                return types.contacts.ResolvedPeer(peer=types.PeerUser(entity.id), chats=[], users=[entity])
            return types.contacts.ResolvedPeer(peer=types.PeerChannel(entity.id), chats=[entity], users=[])
        if isinstance(request, functions.channels.JoinChannelRequest):
            self.entities[_peer(request.channel)] = request.channel
            return types.Updates(updates=[], users=[], chats=[request.channel], date=_now(), seq=0)
        raise AssertionError(f"Unexpected Telegram request {type(request).__name__}")

    async def get_input_entity(self, peer):
        if peer in self.history or peer in self.entities:
            return peer
        raise ValueError(f"Could not find the input entity for {peer}")

    async def get_entity(self, peer):
        if peer in self.entities:
            return self.entities[peer]
        raise ValueError(f"Could not find the entity for {peer}")

    async def iter_messages(self, entity, min_id=0, limit=None):
        self.iter_calls.append((entity, min_id))
        for message in sorted(self.history.get(entity, []), key=lambda m: m.id, reverse=True):
            if message.id > min_id:
                yield message

    async def get_messages(self, entity, ids=None):
        by_id = {m.id: m for m in self.history.get(entity, [])}
        if isinstance(ids, list):
            return [by_id.get(i) for i in ids]
        return by_id.get(ids)

    async def get_participants(self, entity, filter=None):
        assert isinstance(filter, types.ChannelParticipantsAdmins), filter
        self.participant_calls += 1
        admins = self.admins[entity]
        if isinstance(admins, Exception):
            raise admins
        return [SimpleNamespace(id=user_id) for user_id in admins]

    def add_event_handler(self, callback, event):
        self.handlers.append(event)


class FakeExtractor:
    """Stands in for the LLM: returns the scripted result of the first rule whose
    keyword appears in the text block, and records every call."""

    def __init__(self, rules=()):
        self.rules = list(rules)  # (keyword, LLMExtractionResult)
        self.calls = []  # (text_block, schedule context)

    async def __call__(self, raw_text, current_context=None):
        self.calls.append((raw_text, current_context or {}))
        for keyword, result in self.rules:
            if keyword in raw_text:
                return result.model_copy(deep=True)
        return LLMExtractionResult()


def _create(title: str, *, days: int = 2, company_name=None) -> ExtractedEvent:
    return ExtractedEvent(
        action="CREATE_EVENT",
        event_type="FORM_SUBMISSION",
        title=title,
        end_time=_now() + timedelta(days=days),
        company_name=company_name,
    )


async def _settle(service: TelegramIngestionService, timeout: float = 5.0) -> None:
    """Wait until background tasks are done and every debounce buffer has flushed."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        busy = [task for task in service._background_tasks if not task.done()]
        debouncer = service._debouncer
        if not busy and not debouncer._slots and not any(debouncer._queued.values()):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("ingestion did not settle")


async def _company(name, *, chat_id=None, env="MAIN", mode="ELIGIBLE", roles=("ELIGIBLE",), active=True) -> int:
    async with _TestSession() as db:
        company = Company(
            name=name,
            normalized_name=name.lower(),
            environment_type=env,
            subchannel_id=chat_id,
            tracking_mode=mode,
            is_active=active,
        )
        db.add(company)
        await db.flush()
        for role_mode in roles:
            db.add(RoleTrack(company_id=company.id, role_name=f"{name} role", tracking_mode=role_mode))
        await db.commit()
        return company.id


async def _get_company(*, chat_id=None, company_id=None):
    async with _TestSession() as db:
        if company_id is not None:
            return await db.get(Company, company_id)
        result = await db.execute(select(Company).where(Company.subchannel_id == chat_id))
        return result.scalars().first()


async def _roles_of(company_id: int):
    async with _TestSession() as db:
        result = await db.execute(select(RoleTrack).where(RoleTrack.company_id == company_id))
        return list(result.scalars().all())


async def _event_titles(company_id: int):
    async with _TestSession() as db:
        result = await db.execute(select(TimelineEvent.title).where(TimelineEvent.company_id == company_id))
        return sorted(result.scalars().all())


async def _ledger_rows(chat_id: int) -> dict:
    async with _TestSession() as db:
        rows = await db.execute(select(MessageLedger).where(MessageLedger.chat_id == chat_id))
        return {row.message_id: row for row in rows.scalars().all()}


async def _set_feeds(main=None, test=None) -> None:
    """The single Settings row (main/test feed IDs typed into the app)."""
    async with _TestSession() as db:
        config = (await db.execute(select(SystemConfig))).scalars().first()
        if config is None:
            config = SystemConfig()
            db.add(config)
        config.main_feed_id, config.test_feed_id = main, test
        await db.commit()


def _migrate(connection) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["connection"] = connection
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_link_parsing() -> None:
    P, U = join_policy.PRIVATE, join_policy.PUBLIC
    followed = {
        "Join https://t.me/+AbCdEfGh1234.": [(P, "AbCdEfGh1234")],
        "[Join here](https://t.me/+AbCdEfGh1234)": [(P, "AbCdEfGh1234")],
        "**https://t.me/+AbCdEfGh1234**": [(P, "AbCdEfGh1234")],
        "old style t.me/joinchat/ZyXwVuTs9876": [(P, "ZyXwVuTs9876")],
        "https://telegram.me/+AbCdEfGh1234 http://www.telegram.dog/+AbCdEfGh1234": [(P, "AbCdEfGh1234")],
        "Channel: https://t.me/Amazon_SDE_2026 !": [(U, "amazon_sde_2026")],
        "preview t.me/s/amazon_sde_2026": [(U, "amazon_sde_2026")],
        "https://amazon_jobs.t.me/": [(U, "amazon_jobs")],
        "tg://join?invite=AbCdEfGh1234": [(P, "AbCdEfGh1234")],
        "tg://resolve?domain=CompanyChan": [(U, "companychan")],
        "Two: t.me/+AaaaBbbb1111 and t.me/+CcccDddd2222": [(P, "AaaaBbbb1111"), (P, "CcccDddd2222")],
    }
    never_followed = [
        "permalink https://t.me/amazon_sde_2026/45",
        "private permalink https://t.me/c/1234567890/89",
        "forum topic t.me/amazon_sde_2026/12/34",
        "phone number t.me/+919876543210",
        "bot deep link https://t.me/placement_bot?start=ref123",
        "mini app t.me/placement_bot/app?startapp=x",
        "livestream t.me/amazon_sde_2026?livestream",
        "chat folder t.me/addlist/AbCdEfGh1234",
        "stickers t.me/addstickers/SomePack, share t.me/share/url?url=x",
        "proxy t.me/proxy?server=1.2.3.4&port=443, login t.me/login/12345",
        "mention [Ravi](tg://user?id=123456)",
        "bot start tg://resolve?domain=placement_bot&start=1",
        "email hr@company.t.me",
        "not a link: abct.me/abcd",
        "too short t.me/abc",
    ]
    for text, expected in followed.items():
        got = [(link.kind, link.token) for link in join_policy.find_invite_links(text)]
        assert got == expected, (text, got)
    for text in never_followed:
        assert join_policy.find_invite_links(text) == [], text
    assert join_policy.InviteLink(P, "AbCdEfGh1234").masked() == "t.me/+AbCd…"  # hashes are not logged

    print("  [1] ✓ Join policy links: invites and public channels followed; permalinks, phone links, bot/app links, folders and features never")


def test_chat_checks() -> None:
    broadcast = _channel(3000000001, "Acme Careers")
    supergroup = _channel(3000000002, "Acme Discussion", megagroup=True)
    feed = _channel(1000000001, "Placement Feed")
    person = types.User(id=42, first_name="Ravi")
    bot = types.User(id=43, first_name="Placement Bot", bot=True)
    basic_group = types.Chat(
        id=44, title="Friends", photo=types.ChatPhotoEmpty(), participants_count=5, date=_now(), version=1
    )
    roots = {MAIN_FEED}

    assert join_policy.check_chat(broadcast, roots) is None
    assert join_policy.check_chat(supergroup, roots) is None
    assert join_policy.check_chat(person, roots) == join_policy.PERSON_OR_BOT
    assert join_policy.check_chat(bot, roots) == join_policy.PERSON_OR_BOT
    assert join_policy.check_chat(basic_group, roots) == join_policy.NOT_A_CHANNEL
    assert join_policy.check_chat(None, roots) == join_policy.NOT_A_CHANNEL
    assert join_policy.check_chat(feed, roots) == join_policy.ROOT_FEED

    def invite(**flags):
        return types.ChatInvite(title="x", photo=types.PhotoEmpty(id=0), participants_count=10, color=0, **flags)

    assert join_policy.check_invite(invite(channel=True, broadcast=True), roots) is None
    assert join_policy.check_invite(invite(channel=True, megagroup=True), roots) is None
    assert join_policy.check_invite(invite(), roots) == join_policy.NOT_A_CHANNEL  # a basic group
    assert join_policy.check_invite(
        invite(channel=True, broadcast=True, request_needed=True), roots
    ) == join_policy.NEEDS_APPROVAL
    assert join_policy.check_invite(types.ChatInviteAlready(chat=feed), roots) == join_policy.ROOT_FEED
    assert join_policy.check_invite(types.ChatInviteAlready(chat=broadcast), roots) is None
    assert join_policy.check_invite(
        types.ChatInvitePeek(chat=basic_group, expires=0), roots
    ) == join_policy.NOT_A_CHANNEL

    assert join_policy.is_channel_id(MAIN_FEED) and join_policy.is_channel_id(str(MAIN_FEED))
    for not_a_channel in (123456789, -987654, 0, "abc", None):
        assert not join_policy.is_channel_id(not_a_channel), not_a_channel
    assert join_policy.is_broadcast_channel(broadcast)
    for not_broadcast in (supergroup, person, basic_group, None):
        assert not join_policy.is_broadcast_channel(not_broadcast)

    print("  [2] ✓ Join policy chats: channels and supergroups only — never persons, bots, basic groups, root feeds or approval-only invites")


async def test_short_messages_reach_the_llm() -> None:
    feed = _channel(1000000001, "Placement Feed")
    sub = _peer(_channel(3100000001, "Slots Channel"))
    service = TelegramIngestionService(FakeTelegram(), {"MAIN"})
    service.root_feed_ids[MAIN_FEED] = "MAIN"
    service.active_subchannel_ids.add(sub)
    batches = []

    async def handler(source, messages):
        batches.append((source.chat_id, [m["raw_text"] for m in messages]))

    await service.set_payload_handler(handler)
    sub_messages = [
        _msg(1, "Slot 1"), _msg(2, "Room 204"), _msg(3, "Link active"),
        _msg(4, "   \n "), _msg(5, None),  # blank text; media without a caption
    ]
    feed_messages = [_msg(901, "ok"), _msg(902, "​⁣")]
    for message in sub_messages:
        await service._process_event(_event(sub, message), is_edit=False)
    for message in feed_messages:
        await service._process_event(_event(MAIN_FEED, message, chat=feed), is_edit=False)
    await _settle(service)

    assert sorted(batches) == sorted([(sub, ["Slot 1", "Room 204", "Link active"]), (MAIN_FEED, ["ok"])]), batches
    sub_rows, feed_rows = await _ledger_rows(sub), await _ledger_rows(MAIN_FEED)
    assert {i: sub_rows[i].status for i in sub_rows} == {1: "DONE", 2: "DONE", 3: "DONE", 4: "SKIPPED", 5: "SKIPPED"}
    assert sub_rows[4].reason == sub_rows[5].reason == feed_rows[902].reason == "empty"
    assert sub_rows[4].content_hash == content_hash("   \n ")
    assert feed_rows[901].status == "DONE"

    print("  [3] ✓ Pre-filter in both tiers: 'Slot 1', 'Room 204', 'Link active', 'ok' reach the LLM; only blank messages are SKIPPED 'empty'")


class _MessageWithReactions(SimpleNamespace):
    """A message whose reactions must never be read."""

    @property
    def reactions(self):
        raise AssertionError("message.reactions was read")


async def test_reactions_are_never_read() -> None:
    client = FakeTelegram()
    service = TelegramIngestionService(client, {"MAIN"})
    service.register_handlers()
    assert client.handlers == [events.NewMessage, events.MessageEdited], client.handlers

    # Telethon never turns reaction updates into the two subscribed events.
    peer = types.PeerChannel(3200000001)
    reaction_updates = [
        types.UpdateMessageReactions(peer=peer, msg_id=1, reactions=types.MessageReactions(results=[])),
        types.UpdateBotMessageReaction(
            peer=peer, msg_id=1, date=_now(), actor=types.PeerUser(5),
            old_reactions=[], new_reactions=[types.ReactionEmoji(emoticon="\U0001f525")], qts=1,
        ),
        types.UpdateBotMessageReactions(peer=peer, msg_id=1, date=_now(), reactions=[], qts=1),
    ]
    for update_ in reaction_updates:
        for builder in (events.NewMessage, events.MessageEdited):
            assert builder.build(update_) is None, (builder, type(update_).__name__)

    # Only the text (or caption) is extracted.
    message = _msg(1, "OA at 5 PM", cls=_MessageWithReactions)
    payload = _build_message_dict(message, is_edit=False)
    assert set(payload) == {
        "id", "text", "raw_text", "content_hash", "date", "is_edit", "reply_to_msg_id", "parent_context", "company_id"
    }, set(payload)

    # Edit updates that leave the text unchanged are ignored.
    chat = _peer(_channel(3200000001, "Reactions Channel"))
    service.active_subchannel_ids.add(chat)
    seen = []

    async def handler(source, messages):
        seen.append([(m["id"], m["raw_text"], m["is_edit"]) for m in messages])

    await service.set_payload_handler(handler)
    await service._process_event(_event(chat, _msg(1, "OA at 5 PM", cls=_MessageWithReactions)), is_edit=False)
    await service._process_event(_event(chat, _msg(2, "Room 204", cls=_MessageWithReactions)), is_edit=False)
    await _settle(service)
    assert seen == [[(1, "OA at 5 PM", False), (2, "Room 204", False)]], seen
    before = await _ledger_rows(chat)

    for update_ in (
        _msg(1, "OA at 5 PM", cls=_MessageWithReactions),  # never edited: a reaction or view update
        _msg(2, "Room 204", edit_age=timedelta(minutes=1), cls=_MessageWithReactions),  # edited, same text
        _msg(99, "Old post", edit_age=timedelta(days=3), cls=_MessageWithReactions),  # never processed, old edit
    ):
        await service._process_event(_event(chat, update_), is_edit=True)
    await _settle(service)
    after = await _ledger_rows(chat)
    assert len(seen) == 1 and service._debouncer.pending_ids(chat) == set()
    assert set(after) == {1, 2} and all(after[i].processed_at == before[i].processed_at for i in (1, 2))

    # A real edit is processed again, as an edit.
    await service._process_event(
        _event(chat, _msg(1, "OA moved to 6 PM", edit_age=timedelta(seconds=5), cls=_MessageWithReactions)),
        is_edit=True,
    )
    await _settle(service)
    assert seen[-1] == [(1, "OA moved to 6 PM", True)], seen
    assert (await _ledger_rows(chat))[1].content_hash == content_hash("OA moved to 6 PM")

    print("  [4] ✓ Reactions: only NewMessage/MessageEdited are subscribed, reactions are never read, and edit updates with unchanged text are ignored")


async def test_invite_post_joins_and_attributes() -> None:
    client = FakeTelegram()
    feed = _channel(1000000001, "NITK Placements")
    client.entities[MAIN_FEED] = feed
    amazon = _channel(2000000001, "Amazon SDE 2026 | NITK")
    amazon_id = _peer(amazon)
    client.add_invite("AmazonInvite01", amazon)
    client.history[amazon_id] = [
        _msg(1, "Welcome to the Amazon channel", age=timedelta(hours=30)),
        _msg(2, "Amazon OA on 27 Oct, 7 PM", age=timedelta(hours=3)),
        _msg(3, "PPT link will be shared soon", age=timedelta(minutes=10)),
    ]
    await _set_feeds(main=MAIN_FEED)
    extractor = FakeExtractor([
        ("Amazon SDE-1 drive", LLMExtractionResult(
            company_name="Amazon",
            actions=[_create("Amazon registration", company_name="Amazon Web Services")],
        )),
        ("OA link", LLMExtractionResult(company_name="Amazon", actions=[_create("Amazon OA", days=3)])),
    ])
    service = TelegramIngestionService(client, {"MAIN"})
    await service.initialize()
    await service.set_payload_handler(worker.dispatch_to_ai)

    with patch("app.ai.extractor.extract_placement_actions", extractor):
        post = _msg(100, "Amazon SDE-1 drive. Register by 25 Oct 11:59 PM. Join https://t.me/+AmazonInvite01")
        client.history[MAIN_FEED] = [post]
        await service._process_event(_event(MAIN_FEED, post, chat=feed), is_edit=False)
        await _settle(service)

        assert client.request_names == ["CheckChatInviteRequest", "ImportChatInviteRequest"], client.request_names
        company = await _get_company(chat_id=amazon_id)
        assert company.name == "Amazon", company.name  # the extracted name, not the channel title
        assert (company.environment_type, company.tracking_mode) == ("MAIN", "SILENT_TRACK")
        assert (company.invite_token, company.main_feed_msg_id) == ("AmazonInvite01", 100)
        assert [(r.role_name, r.is_triaged) for r in await _roles_of(company.id)] == [("Default Role", False)]
        # The post's event belongs to the linked company, whatever name the action carried.
        assert await _event_titles(company.id) == ["Amazon registration"]
        assert amazon_id in service.active_subchannel_ids
        feed_rows = await _ledger_rows(MAIN_FEED)
        assert feed_rows[100].status == "DONE" and feed_rows[100].content_hash == content_hash(post.text)
        channel_rows = await _ledger_rows(amazon_id)
        assert sorted(channel_rows) == [2, 3] and all(r.status == "DONE" for r in channel_rows.values())
        assert client.iter_calls == [(amazon_id, 0)]  # one bounded 24 h backfill read

        # A re-post of the same invite: no Telegram request, extracted with the company.
        requests_before = len(client.requests)
        repost = _msg(101, "Reminder: registration closes tonight https://t.me/+AmazonInvite01")
        client.history[MAIN_FEED].append(repost)
        await service._process_event(_event(MAIN_FEED, repost, chat=feed), is_edit=False)
        await _settle(service)
        assert len(client.requests) == requests_before
        assert extractor.calls[-1][1].get("company_id") == company.id
        assert (await _get_company(company_id=company.id)).main_feed_msg_id == 101

        # A reply to the invite post is attributed to its company.
        reply = _msg(102, "OA link: https://oa.example.com, closes 26 Oct 6 PM", reply_to=101)
        client.history[MAIN_FEED].append(reply)
        await service._process_event(_event(MAIN_FEED, reply, chat=feed), is_edit=False)
        await _settle(service)
        text_block, context = extractor.calls[-1]
        assert context.get("company_id") == company.id and "[CONTEXT:" in text_block
        assert await _event_titles(company.id) == ["Amazon OA", "Amazon registration"]
        assert len(client.requests) == requests_before

    print("  [5] ✓ Invite post: previewed then joined, linked under the extracted name, extracted with its company, 24 h backfill; re-posts and replies attributed with no Telegram request")


async def test_invite_refusals_and_ignored_companies() -> None:
    client = FakeTelegram()
    feed = _channel(1000000001, "NITK Placements", username="nitk_placements")
    client.entities[MAIN_FEED] = feed
    client.usernames["nitk_placements"] = feed
    client.usernames["placement_helper_bot"] = types.User(id=901, first_name="Helper", bot=True)
    client.usernames["ravi_spoc"] = types.User(id=902, first_name="Ravi")
    client.invites["GroupInvite001"] = types.ChatInvite(
        title="Friends", photo=types.PhotoEmpty(id=0), participants_count=4, color=0
    )
    client.add_invite("ApprovalInvite1", _channel(2100000001, "Approval Channel"), request_needed=True)
    client.invites["ExpiredInvite01"] = errors.InviteHashExpiredError(request=None)
    globex = _channel(2100000002, "Globex Careers")
    client.add_invite("GlobexInvite001", globex)
    globex_id = await _company("Globex", chat_id=_peer(globex), mode="IGNORED", roles=("IGNORED",))
    async with _TestSession() as db:
        await crud.record_invite_post(db, globex_id, "GlobexInvite001", 200)
    await _set_feeds(main=MAIN_FEED)
    extractor = FakeExtractor()
    service = TelegramIngestionService(client, {"MAIN"})
    await service.initialize()
    await service.set_payload_handler(worker.dispatch_to_ai)

    refused = {
        201: "Register via https://t.me/placement_helper_bot",
        202: "Queries: https://t.me/ravi_spoc",
        203: "Group: https://t.me/+GroupInvite001",
        204: "Approval needed: https://t.me/+ApprovalInvite1",
        205: "Expired: https://t.me/+ExpiredInvite01",
        206: "Call t.me/+919876543210, or see https://t.me/amazon_sde/45",
        207: "Our own feed: https://t.me/nitk_placements",
    }
    with patch("app.ai.extractor.extract_placement_actions", extractor):
        for message_id, text in refused.items():
            post = _msg(message_id, text)
            client.history.setdefault(MAIN_FEED, []).append(post)
            await service._process_event(_event(MAIN_FEED, post, chat=feed), is_edit=False)
        await _settle(service)

        names = client.request_names
        assert "ImportChatInviteRequest" not in names and "JoinChannelRequest" not in names, names
        assert names.count("ResolveUsernameRequest") == 3 and names.count("CheckChatInviteRequest") == 3, names
        rows = await _ledger_rows(MAIN_FEED)
        assert all(rows[i].status == "DONE" for i in refused), {i: rows[i].status for i in refused}
        assert len(extractor.calls) == len(refused) and not any(ctx for _, ctx in extractor.calls)

        # An IGNORED company: its invite re-post and a reply to its invite post
        # are skipped — no Telegram request, no LLM call.
        before = (len(client.requests), len(extractor.calls))
        for post in (_msg(210, "Globex update https://t.me/+GlobexInvite001"), _msg(211, "Globex test on Friday", reply_to=200)):
            client.history[MAIN_FEED].append(post)
            await service._process_event(_event(MAIN_FEED, post, chat=feed), is_edit=False)
        await _settle(service)
        assert (len(client.requests), len(extractor.calls)) == before
        rows = await _ledger_rows(MAIN_FEED)
        assert rows[210].reason == rows[211].reason == "company_ignored"
        assert (await _get_company(company_id=globex_id)).tracking_mode == "IGNORED"

    print("  [6] ✓ Refused links (bot, person, basic group, approval, expired, phone, permalink, root feed) are never joined; posts still extracted; IGNORED companies skipped")


async def test_flood_wait_is_retried() -> None:
    client = FakeTelegram()
    feed = _channel(1000000001, "NITK Placements")
    client.entities[MAIN_FEED] = feed
    flipkart, myntra = _channel(2200000001, "Flipkart GRiD"), _channel(2200000002, "Myntra Careers")
    client.add_invite("FlipkartInvite1", flipkart)
    client.add_invite("MyntraInvite001", myntra)
    client.import_errors = [errors.FloodWaitError(request=None, capture=600)]
    await _set_feeds(main=MAIN_FEED)
    extractor = FakeExtractor([("Flipkart", LLMExtractionResult(company_name="Flipkart"))])
    service = TelegramIngestionService(client, {"MAIN"})
    await service.initialize()
    await service.set_payload_handler(worker.dispatch_to_ai)

    with patch("app.ai.extractor.extract_placement_actions", extractor):
        first, second = _msg(300, "Flipkart drive https://t.me/+FlipkartInvite1"), _msg(301, "Myntra drive https://t.me/+MyntraInvite001")
        client.history[MAIN_FEED] = [first, second]
        started = _now()
        await service._process_event(_event(MAIN_FEED, first, chat=feed), is_edit=False)
        await _settle(service)
        row = (await _ledger_rows(MAIN_FEED))[300]
        assert row.status == "FAILED" and row.attempts == 1 and "FloodWaitError" in row.reason
        assert row.next_attempt_at - started >= timedelta(seconds=595)  # not before the flood wait ends

        requests_before = len(client.requests)
        await service._process_event(_event(MAIN_FEED, second, chat=feed), is_edit=False)
        await _settle(service)
        assert len(client.requests) == requests_before  # joins paused: no request during the wait
        row = (await _ledger_rows(MAIN_FEED))[301]
        assert row.status == "FAILED" and row.next_attempt_at - started >= timedelta(seconds=590)
        assert "flood wait" in (service.last_error or "").lower()
        assert not extractor.calls  # nothing extracted until the channel is joined

        # The wait passes: the retry loop re-runs both joins.
        service._joins_blocked_until = None
        async with _TestSession() as db:
            await db.execute(
                update(MessageLedger)
                .where(MessageLedger.chat_id == MAIN_FEED, MessageLedger.message_id.in_([300, 301]))
                .values(next_attempt_at=_now() - timedelta(seconds=1))
            )
            await db.commit()
        assert await service.retry_failed() == 2
        await _settle(service)
        rows = await _ledger_rows(MAIN_FEED)
        assert rows[300].status == rows[301].status == "DONE"
        assert (await _get_company(chat_id=_peer(flipkart))).name == "Flipkart"
        assert (await _get_company(chat_id=_peer(myntra))).name == "Myntra Careers"  # no name extracted: channel title
        assert {_peer(flipkart), _peer(myntra)} <= service.active_subchannel_ids

    print("  [7] ✓ Flood wait: the post is FAILED until the wait ends, other joins pause without a request, and the retry loop joins and links both channels")


async def test_joins_are_paced() -> None:
    listener_module._JOIN_INTERVAL_SECONDS = 0.3
    try:
        client = FakeTelegram()
        feed = _channel(1000000001, "NITK Placements")
        client.entities[MAIN_FEED] = feed
        one, two = _channel(2300000011, "Paced One"), _channel(2300000012, "Paced Two")
        client.add_invite("PacedInvite0001", one)
        client.add_invite("PacedInvite0002", two)
        await _set_feeds(main=MAIN_FEED)
        extractor = FakeExtractor()
        service = TelegramIngestionService(client, {"MAIN"})
        await service.initialize()
        await service.set_payload_handler(worker.dispatch_to_ai)
        with patch("app.ai.extractor.extract_placement_actions", extractor):
            post = _msg(400, "Two drives: https://t.me/+PacedInvite0001 and https://t.me/+PacedInvite0002")
            client.history[MAIN_FEED] = [post]
            await service._process_event(_event(MAIN_FEED, post, chat=feed), is_edit=False)
            await _settle(service)
            checks = [at for at, request in client.requests if isinstance(request, functions.messages.CheckChatInviteRequest)]
            assert len(checks) == 2 and checks[1] - checks[0] >= 0.28, checks
            # Several channels: each linked under its own title; the post is a plain announcement.
            assert (await _get_company(chat_id=_peer(one))).name == "Paced One"
            assert (await _get_company(chat_id=_peer(two))).name == "Paced Two"
            assert extractor.calls and not extractor.calls[-1][1]
            assert {_peer(one), _peer(two)} <= service.active_subchannel_ids

            # A reply to a post about several companies is not attributed to any of them.
            reply = _msg(401, "Deadline for both is Friday", reply_to=400)
            client.history[MAIN_FEED].append(reply)
            await service._process_event(_event(MAIN_FEED, reply, chat=feed), is_edit=False)
            await _settle(service)
            assert not extractor.calls[-1][1].get("company_id")
    finally:
        listener_module._JOIN_INTERVAL_SECONDS = 0

    print("  [8] ✓ Joins are paced (one invite link per interval); a post with several channels links each under its title; replies to it stay unattributed")


async def test_attribution_never_crosses_environments() -> None:
    test_feed_id = -1001000000007
    client = FakeTelegram()
    test_feed = _channel(1000000007, "Test Feed")
    client.entities[test_feed_id] = test_feed
    real = _channel(2350000001, "Wayne Enterprises Careers")
    client.add_invite("WayneInvite0001", real)
    client.joined.add("WayneInvite0001")  # already a member: the channel is linked to a MAIN company
    wayne = await _company("Wayne Enterprises", chat_id=_peer(real))
    await _set_feeds(main=MAIN_FEED, test=test_feed_id)
    extractor = FakeExtractor([("Wayne", LLMExtractionResult(company_name="Wayne Enterprises", actions=[_create("Wayne test event")]))])
    service = TelegramIngestionService(client, {"MAIN", "TEST"})
    await service.initialize()
    await service.set_payload_handler(worker.dispatch_to_ai)
    with patch("app.ai.extractor.extract_placement_actions", extractor):
        post = _msg(1, "Testing with Wayne https://t.me/+WayneInvite0001")
        client.history[test_feed_id] = [post]
        await service._process_event(_event(test_feed_id, post, chat=test_feed), is_edit=False)
        await _settle(service)
    assert client.request_names == ["CheckChatInviteRequest"]  # already a member: nothing joined
    assert extractor.calls and not extractor.calls[-1][1].get("company_id")  # not attributed to the MAIN company
    assert (await _get_company(company_id=wayne)).main_feed_msg_id is None

    print("  [15] ✓ A TEST-feed post linking a MAIN company's channel is not attributed to it")


async def test_supergroups_keep_only_admin_posts() -> None:
    client = FakeTelegram()
    group = _channel(2400000001, "Hooli Discussion", megagroup=True)
    group_id = _peer(group)
    client.history[group_id] = []
    client.admins[group_id] = [501]
    await _company("Hooli", chat_id=group_id)
    service = TelegramIngestionService(client, {"MAIN"})
    service.active_subchannel_ids.add(group_id)
    kept = []

    async def handler(source, messages):
        kept.extend(m["id"] for m in messages)

    await service.set_payload_handler(handler)
    messages = [
        _msg(1, "Hooli OA tomorrow 10 AM", post=False, sender=types.PeerUser(501)),  # admin
        _msg(2, "is the OA tomorrow?", post=False, sender=types.PeerUser(777)),  # member
        _msg(3, "Interview slots are out", post=False, sender=types.PeerChannel(2400000001)),  # anonymous admin
        _msg(4, "Join my channel", post=False, sender=types.PeerChannel(2400000002)),  # posting as another channel
        _msg(5, "same doubt", post=False, sender=types.PeerUser(778)),
    ]
    for message in messages:
        await service._process_event(_event(group_id, message), is_edit=False)
    await _settle(service)
    assert sorted(kept) == [1, 3], kept
    rows = await _ledger_rows(group_id)
    assert all(rows[i].reason == "non_admin_sender" for i in (2, 4, 5))
    assert client.participant_calls == 1  # the admin list is cached

    # If the admin list can't be read, messages are kept (recall first).
    other = _channel(2400000003, "Pied Piper Group", megagroup=True)
    other_id = _peer(other)
    client.history[other_id] = []
    client.admins[other_id] = RuntimeError("CHAT_ADMIN_REQUIRED")
    service.active_subchannel_ids.add(other_id)
    await service._process_event(_event(other_id, _msg(7, "Pied Piper test at 3", post=False, sender=types.PeerUser(777))), is_edit=False)
    await _settle(service)
    assert 7 in kept

    print("  [9] ✓ Supergroups: admin and anonymous-admin posts kept, members' messages SKIPPED 'non_admin_sender'; admin list cached; unreadable list keeps messages")


async def test_root_feeds_must_be_broadcast_channels() -> None:
    client = FakeTelegram()
    await _set_feeds(main=123456789, test=-987654)  # a person's ID and a basic group's ID
    service = TelegramIngestionService(client, {"MAIN", "TEST"})
    await service.initialize()
    assert service.root_feed_ids == {}
    assert "not a channel ID" in (service.last_error or "")

    group_feed = _channel(1000000005, "Students Chat", megagroup=True)
    group_feed_id = _peer(group_feed)
    await _set_feeds(main=group_feed_id)
    await service.refresh_monitored()
    assert service.root_feed_ids == {group_feed_id: "MAIN"}
    post = _msg(1, "Join https://t.me/+SomeInvite0001")
    client.history[group_feed_id] = [post]
    service.register_handlers()
    await service.set_payload_handler(worker.dispatch_to_ai)
    await service._process_event(_event(group_feed_id, post, chat=group_feed), is_edit=False)
    await _settle(service)
    assert not client.requests and await _ledger_rows(group_feed_id) == {}
    assert "not a broadcast channel" in (service.last_error or "")
    await service.catch_up()
    assert client.iter_calls == []  # a refused feed is never read

    good_feed = _channel(1000000006, "Placement Feed")
    await _set_feeds(main=_peer(good_feed))
    await service.refresh_monitored()
    seen = []

    async def handler(source, messages):
        seen.append(source.context)

    await service.set_payload_handler(handler)
    await service._process_event(_event(_peer(good_feed), _msg(2, "Hello everyone", post=True), chat=good_feed), is_edit=False)
    await _settle(service)
    assert seen == ["main_feed"]

    print("  [10] ✓ Root feeds: person/basic-group IDs refused at refresh, a supergroup feed refused at its first message (never read or joined from); broadcast channels accepted")


async def test_debouncer_max_wait_and_ordering() -> None:
    loop = asyncio.get_running_loop()
    debouncer = ChannelDebouncer()
    flushes = []

    async def record(chat_id, messages):
        flushes.append((loop.time(), [m["id"] for m in messages]))

    started = loop.time()
    for i in range(10):  # a steady stream: never 0.1 s of quiet
        await debouncer.add_message(7, {"id": i, "date": i}, record, delay=0.1, max_wait=0.25)
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.4)
    first_at, first_ids = flushes[0]
    assert first_at - started <= 0.4 and 0 < len(first_ids) < 10, flushes
    assert sorted(i for _, ids in flushes for i in ids) == list(range(10))

    active = peak = 0

    async def slow(chat_id, messages):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.1)
        active -= 1

    ordered = ChannelDebouncer()
    await ordered.add_message(8, {"id": 1, "date": 1}, slow, delay=0)
    await asyncio.sleep(0.02)  # batch 1 is being handled
    await ordered.add_message(8, {"id": 2, "date": 2}, slow, delay=0)
    await asyncio.sleep(0.02)  # batch 2 waits for batch 1
    assert ordered.pending_ids(8) == {1, 2}  # neither counts as unprocessed for catch-up
    await asyncio.sleep(0.3)
    assert peak == 1 and ordered.pending_ids(8) == set()

    print("  [11] ✓ Debouncer: max-wait flushes a busy chat; batches of one chat never overlap; queued messages still count as pending")


async def test_batches_hold_at_most_40_messages() -> None:
    chat = _peer(_channel(2500000001, "Bulk Channel"))
    service = TelegramIngestionService(FakeTelegram(), {"MAIN"})
    service.active_subchannel_ids.add(chat)
    batches = []

    async def handler(source, messages):
        batches.append((len(messages), messages[0]["id"], messages[-1]["id"]))

    await service.set_payload_handler(handler)
    for i in range(1, 96):
        await service._process_event(_event(chat, _msg(i, f"Update {i}")), is_edit=False)
    await _settle(service)
    assert batches == [(40, 1, 40), (40, 41, 80), (15, 81, 95)], batches
    rows = await _ledger_rows(chat)
    assert len(rows) == 95 and all(row.status == "DONE" for row in rows.values())

    print("  [12] ✓ 95 buffered messages → LLM calls of 40, 40 and 15, in order, each recorded DONE")


async def test_catch_up_replays_offline_edits() -> None:
    chat = _peer(_channel(2600000001, "Edits Channel"))
    client = FakeTelegram()
    client.history[chat] = [
        _msg(1, "OA moved to 6 PM", edit_age=timedelta(hours=1)),  # edited while offline
        _msg(2, "Room 204", edit_age=timedelta(hours=2)),  # edited, but this text was handled
        _msg(3, "PPT at 4", edit_age=timedelta(minutes=30)),  # handled before hashes were stored
        _msg(4, "Interview list is out"),  # new
    ]
    await ledger.record(chat, [1], ledger.DONE, content_hashes={1: content_hash("OA at 5 PM")})
    await ledger.record(chat, [2], ledger.DONE, content_hashes={2: content_hash("Room 204")})
    await ledger.record(chat, [3], ledger.DONE)
    service = TelegramIngestionService(client, {"MAIN"})
    service.active_subchannel_ids.add(chat)
    routed = []

    async def capture(chat_id, message, is_edit=False):
        routed.append((message.id, is_edit))

    service._route_message = capture
    assert await service.catch_up([chat]) == 2
    assert routed == [(1, True), (4, False)], routed

    print("  [13] ✓ Catch-up replays edits made while offline (as edits) and skips messages whose text is unchanged")


async def test_dispatcher_source_company_and_naming() -> None:
    def link(raw_id, title, token):
        return SubchannelLink(
            chat_id=_peer(_channel(raw_id, title)), title=title, invite_token=token,
            environment="MAIN", main_feed_msg_id=raw_id % 1000,
        )

    async def dispatch(extractor, **kwargs):
        with patch("app.ai.extractor.extract_placement_actions", extractor):
            async with _TestSession() as db:
                await dispatcher.process_and_dispatch(
                    chat_id=kwargs.pop("chat_id", MAIN_FEED),
                    context_header=kwargs.pop("context_header", "main_feed"),
                    messages=[{"text": kwargs.pop("text", "Drive update"), "label": "[1]"}],
                    db=db,
                    **kwargs,
                )

    # (a) An authoritative company: per-action company names are not followed.
    zeta, omega = await _company("Zeta Labs"), await _company("Omega Corp")
    await dispatch(
        FakeExtractor([("Drive", LLMExtractionResult(company_name="Omega Corp", actions=[_create("Zeta PPT", company_name="Omega Corp")]))]),
        company_id=zeta,
    )
    assert await _event_titles(zeta) == ["Zeta PPT"] and await _event_titles(omega) == []

    # (b) A new channel is linked under the extracted name …
    acme = link(2700000001, "Acme Careers 2026 | NITK", "AcmeInvite00001")
    await dispatch(FakeExtractor([("Drive", LLMExtractionResult(company_name="Acme", actions=[_create("Acme form")]))]), subchannel=acme)
    company = await _get_company(chat_id=acme.chat_id)
    assert (company.name, company.tracking_mode, company.invite_token, company.main_feed_msg_id) == (
        "Acme", "SILENT_TRACK", "AcmeInvite00001", 1
    )
    assert await _event_titles(company.id) == ["Acme form"]

    # (c) … else under its title; (d) a name already linked to another channel
    # falls back to the title; (e) when both are taken, the channel id is added.
    initrode = link(2700000002, "Initrode Hiring Cell", "InitrodeInvite1")
    await dispatch(FakeExtractor(), subchannel=initrode)
    assert (await _get_company(chat_id=initrode.chat_id)).name == "Initrode Hiring Cell"
    acme_intern = link(2700000003, "Acme Internship Channel", "AcmeInvite00002")
    await dispatch(FakeExtractor([("Drive", LLMExtractionResult(company_name="Acme"))]), subchannel=acme_intern)
    assert (await _get_company(chat_id=acme_intern.chat_id)).name == "Acme Internship Channel"
    acme_third = link(2700000004, "Acme Internship Channel", "AcmeInvite00003")
    await dispatch(FakeExtractor([("Drive", LLMExtractionResult(company_name="Acme"))]), subchannel=acme_third)
    assert (await _get_company(chat_id=acme_third.chat_id)).name == f"Acme Internship Channel ({acme_third.chat_id})"

    # (f) A deleted company's name is not reused.
    deleted = await _company("Globex Two", active=False)
    globex = link(2700000005, "Globex Two Hiring", "GlobexInvite002")
    await dispatch(FakeExtractor([("Drive", LLMExtractionResult(company_name="Globex Two"))]), subchannel=globex)
    assert (await _get_company(chat_id=globex.chat_id)).name == "Globex Two Hiring"
    assert (await _get_company(company_id=deleted)).subchannel_id is None

    # (g) An IGNORED company matched by name is linked, but nothing is applied
    # and it stays IGNORED (invariants 1 and 3).
    initech_id = await _company("Initech", mode="IGNORED", roles=("IGNORED",))
    initech = link(2700000006, "Initech Careers", "InitechInvite01")
    await dispatch(FakeExtractor([("Drive", LLMExtractionResult(company_name="Initech", actions=[_create("Initech OA")]))]), subchannel=initech)
    company = await _get_company(company_id=initech_id)
    assert company.subchannel_id == initech.chat_id and company.tracking_mode == "IGNORED"
    assert await _event_titles(initech_id) == []

    # (h) A batch from an IGNORED company's channel never reaches the LLM.
    extractor = FakeExtractor([("Drive", LLMExtractionResult(actions=[_create("Should not exist")]))])
    await dispatch(extractor, chat_id=initech.chat_id, context_header="subchannel")
    assert extractor.calls == [] and await _event_titles(initech_id) == []

    # (i) Main-feed actions naming an IGNORED company are dropped.
    await dispatch(FakeExtractor([("Drive", LLMExtractionResult(company_name="Initech", actions=[_create("Initech PPT", company_name="Initech")]))]))
    assert await _event_titles(initech_id) == []

    print("  [14] ✓ Dispatcher: the source company is authoritative; channels named by extracted name → title → title (id); deleted names not reused; IGNORED companies never changed")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def main() -> None:
    # Schema via the real migrations (0001 baseline + 0002 ledger content hash).
    async with _test_engine.begin() as conn:
        await conn.run_sync(_migrate)

    print("\nRunning Phase 0.4 ingestion verification suite…\n")

    test_link_parsing()
    test_chat_checks()
    await test_short_messages_reach_the_llm()
    await test_reactions_are_never_read()
    await test_invite_post_joins_and_attributes()
    await test_invite_refusals_and_ignored_companies()
    await test_flood_wait_is_retried()
    await test_joins_are_paced()
    await test_supergroups_keep_only_admin_posts()
    await test_root_feeds_must_be_broadcast_channels()
    await test_debouncer_max_wait_and_ordering()
    await test_batches_hold_at_most_40_messages()
    await test_catch_up_replays_offline_edits()
    await test_dispatcher_source_company_and_naming()
    await test_attribution_never_crosses_environments()

    await _test_engine.dispose()
    print("\n✅ Phase 0.4 Ingestion Fixes Verified Successfully!")


if __name__ == "__main__":
    asyncio.run(main())
