"""
app/telegram/listener.py
TelegramIngestionService — the central event router for the placement engine.

Responsibilities:
  • Register NewMessage + MessageEdited handlers on a Telethon client — nothing
    else. Reaction updates (UpdateMessageReactions, UpdateBotMessageReaction…)
    are never subscribed to, reactions are never read, and an edit update that
    leaves the text unchanged (Telegram can re-send a message after a reaction)
    is ignored.
  • Root feeds (main/test; broadcast channels only): an invite post's channels
    are joined under the join policy and the post is extracted together with
    its company; other posts are debounced and extracted, and a reply to an
    invite post is attributed to that post's company.
  • Subchannels: every message with text is debounced and extracted; in a
    supergroup only its admins' posts count.
  • Keep the monitored chats in sync with the database: only ingested
    environments, never effectively-IGNORED or deleted companies
    (PROJECT_STATE §0.1 invariants 2–4).
  • Record every message's outcome in the ledger, catch up on messages (and
    edits) missed while offline, and retry failed messages.
  • Expose set_payload_handler() so the worker can register the dispatcher.

Payload handler signature:
    async def handler(source: MessageSource, messages: List[dict]) -> None
    A batch holds at most _MAX_BATCH_MESSAGES messages of one chat and one
    company. A handler that raises leaves the batch FAILED in the ledger
    (retried later).
"""
import asyncio
import hashlib
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Iterable, List, Optional, Tuple

from sqlalchemy import select
from telethon import TelegramClient, errors, events, utils
from telethon.tl import functions, types

from app import crud, database
from app.config import settings
from app.models import SystemConfig
from app.sources import MessageSource, SubchannelLink
from app.telegram import backfill as _backfill
from app.telegram import join_policy, ledger, pre_filter
from app.telegram.debouncer import ChannelDebouncer

logger = logging.getLogger(__name__)

# Ledger reasons for messages skipped on purpose (besides pre_filter.EMPTY).
COMPANY_IGNORED = "company_ignored"
NON_ADMIN_SENDER = "non_admin_sender"
MESSAGE_DELETED = "message_deleted"

# Chats with no ledger history are bootstrapped from this window (MASTER_SPEC §2).
_BOOTSTRAP_WINDOW = timedelta(hours=24)
# Catch-up re-scans this many messages behind the cursor: a crash can leave
# buffered messages below a later SKIPPED/DONE one.
_CATCHUP_LOOKBACK_MESSAGES = 100
# Pause between chats during catch-up and retries, to stay clear of flood limits.
_CHAT_PACING_SECONDS = 1.0
_ERROR_TEXT_LIMIT = 200
# Messages per LLM call.
_MAX_BATCH_MESSAGES = 40
# At most one invite link is resolved or joined per interval.
_JOIN_INTERVAL_SECONDS = 30.0
# How long a supergroup's admin list is trusted, and how soon a failed read is retried.
_ADMIN_CACHE_TTL = timedelta(hours=24)
_ADMIN_RETRY_TTL = timedelta(minutes=10)

# Join errors that retrying will not fix: the post is extracted without joining.
_PERMANENT_JOIN_ERRORS = tuple(
    getattr(errors, name)
    for name in (
        "InviteHashExpiredError",
        "InviteHashInvalidError",
        "InviteHashEmptyError",
        "InviteRequestSentError",
        "UsernameNotOccupiedError",
        "UsernameInvalidError",
        "ChannelPrivateError",
        "ChannelInvalidError",
    )
    if hasattr(errors, name)
)

# Downstream handler type alias
_PayloadHandler = Callable[[MessageSource, List[dict]], Awaitable[None]]


class _RetryLater(Exception):
    """A join that may work later (a flood wait, a network error): the invite
    post is recorded FAILED and retried with backoff, or after *wait*."""

    def __init__(self, reason: str, wait: Optional[timedelta] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.wait = wait


@dataclass
class _InviteTarget:
    """One link of an invite post, after lookup and — when it is new — a join."""

    link: join_policy.InviteLink
    company_id: Optional[int] = None  # the company already linked to this invite or channel
    ingestible: bool = False
    chat: Any = None  # the channel, when it was resolved or joined just now


def _message_text(message) -> str:
    """Message text or media caption: the only message content the pipeline
    reads — never reactions, view counts or other metadata."""
    return getattr(message, "text", None) or ""


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _build_message_dict(
    message,
    is_edit: bool,
    parent_context: Optional[str] = None,
    company_id: Optional[int] = None,
) -> dict:
    """Normalise a Telethon Message object into a plain dict for pipeline use."""
    raw_text = _message_text(message)
    stitched_text = (
        f"[CONTEXT: {parent_context}]\n{raw_text}" if parent_context else raw_text
    )
    raw_date = _as_utc(getattr(message, "date", None))

    return {
        "id": message.id,
        "text": stitched_text,
        "raw_text": raw_text,
        "content_hash": content_hash(raw_text),
        "date": raw_date.timestamp() if raw_date else 0.0,
        "is_edit": is_edit,
        "reply_to_msg_id": getattr(message, "reply_to_msg_id", None),
        "parent_context": parent_context,
        "company_id": company_id,
    }


def _group_by_company(messages: List[dict]) -> List[Tuple[Optional[int], List[dict]]]:
    """Split a debounced buffer so that every batch belongs to one company (or none)."""
    groups: dict = {}
    for message in messages:
        groups.setdefault(message.get("company_id"), []).append(message)
    return list(groups.items())


def _resolved_entity(resolved):
    """The chat or user a contacts.resolveUsername result points at."""
    peer = getattr(resolved, "peer", None)
    if isinstance(peer, types.PeerChannel):
        return next((c for c in resolved.chats if getattr(c, "id", None) == peer.channel_id), None)
    if isinstance(peer, types.PeerChat):
        return next((c for c in resolved.chats if getattr(c, "id", None) == peer.chat_id), None)
    if isinstance(peer, types.PeerUser):
        return next((u for u in resolved.users if getattr(u, "id", None) == peer.user_id), None)
    return None


def _chat_title(chat) -> str:
    return (
        getattr(chat, "title", None)
        or getattr(chat, "username", None)
        or f"Subchannel {utils.get_peer_id(chat)}"
    )


def _error_text(exc: BaseException) -> str:
    """Exception class plus the first line of its message — never the SQL
    parameters or message text that some errors append on later lines."""
    first_line = (str(exc).splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first_line}"[:_ERROR_TEXT_LIMIT]


class TelegramIngestionService:
    """Stateful event router implementing the Two-Tier Ingestion Workflow (MASTER_SPEC §2/§3)."""

    def __init__(
        self,
        client: TelegramClient,
        environments: Optional[Iterable[str]] = None,
    ) -> None:
        self._client = client
        self._debouncer = ChannelDebouncer()
        self._payload_handler: Optional[_PayloadHandler] = None
        self.environments: frozenset[str] = frozenset(environments or settings.ingest_environments)
        self.active_subchannel_ids: set[int] = set()
        self.root_feed_ids: dict[int, str] = {}  # chat_id -> "MAIN" | "TEST"
        self._background_tasks: set[asyncio.Task] = set()
        self._inflight: dict[int, set[int]] = {}  # message ids being dispatched, per chat
        self._feed_verdicts: dict[int, bool] = {}  # root feed -> is it a broadcast channel
        self._reported_problems: set[Tuple[int, str]] = set()
        # Supergroup -> (admin user ids, or None if unreadable; valid until)
        self._admins: dict[int, Tuple[Optional[frozenset], datetime]] = {}
        self._join_lock = asyncio.Lock()
        self._last_join_at: Optional[float] = None
        self._joins_blocked_until: Optional[datetime] = None
        # Liveness facts the worker persists into worker_status.
        self.last_update_at: Optional[datetime] = None
        self.last_dispatch_ok_at: Optional[datetime] = None
        self.last_catchup_at: Optional[datetime] = None
        self.last_error: Optional[str] = None

    def _track_task(self, task: asyncio.Task) -> asyncio.Task:
        """Maintain an in-memory reference to a background task and log any unhandled errors."""
        self._background_tasks.add(task)

        def _on_done(t: asyncio.Task) -> None:
            self._background_tasks.discard(t)
            if not t.cancelled():
                exc = t.exception()
                if exc:
                    logger.error(
                        "Background task '%s' failed with error: %s",
                        t.get_name(),
                        exc,
                        exc_info=exc,
                    )

        task.add_done_callback(_on_done)
        return task

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Load the root feeds and the monitored subchannels from the database."""
        await self.refresh_monitored(initial=True)

    async def set_payload_handler(self, handler: _PayloadHandler) -> None:
        """Register the downstream classifier. Called by the worker on startup."""
        self._payload_handler = handler
        logger.info("Payload handler registered: %s", getattr(handler, "__qualname__", repr(handler)))

    def register_handlers(self) -> None:
        """Attach the two message handlers — and nothing else: reaction updates
        are never subscribed to."""
        self._client.add_event_handler(self._on_new_message, events.NewMessage)
        self._client.add_event_handler(self._on_edited_message, events.MessageEdited)
        logger.info("Telegram event handlers registered.")

    async def shutdown(self) -> None:
        """Gracefully flush all pending debounce buffers before going offline."""
        logger.info("Flushing debounce buffers on shutdown…")
        await self._debouncer.flush_all(self._debounce_callback)
        if self._background_tasks:
            for task in list(self._background_tasks):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        try:
            await self._process_event(event, is_edit=False)
        except Exception:
            logger.exception("Unhandled error in _on_new_message (chat=%s)", event.chat_id)

    async def _on_edited_message(self, event: events.MessageEdited.Event) -> None:
        try:
            await self._process_event(event, is_edit=True)
        except Exception:
            logger.exception("Unhandled error in _on_edited_message (chat=%s)", event.chat_id)

    # ------------------------------------------------------------------
    # Monitored chats
    # ------------------------------------------------------------------

    def _report_feed_problem(self, chat_id: int, problem: str) -> None:
        """Log a root-feed misconfiguration once and surface it in worker_status."""
        text = f"Root feed {chat_id} {problem}; it is not read. Correct the feed ID in Settings."
        if (chat_id, problem) not in self._reported_problems:
            self._reported_problems.add((chat_id, problem))
            logger.error(text)
        self.last_error = text[:_ERROR_TEXT_LIMIT]

    async def refresh_root_feeds(self) -> dict[int, str]:
        """Collect configured feed channel IDs from Settings and DB SystemConfig,
        keeping only the ingested environments and channel IDs (a person, bot or
        basic group is never a feed). On a database error the previous feeds are
        kept, so a transient failure never stops ingestion."""
        feeds: dict[int, str] = {}
        if settings.MAIN_FEED_CHANNEL_ID:
            feeds[int(settings.MAIN_FEED_CHANNEL_ID)] = "MAIN"
        if settings.TEST_FEED_CHANNEL_ID:
            feeds[int(settings.TEST_FEED_CHANNEL_ID)] = "TEST"
        try:
            async with database.AsyncSessionLocal() as db:
                res = await db.execute(select(SystemConfig))
                cfg = res.scalars().first()
                if cfg:
                    if cfg.main_feed_id:
                        feeds[int(cfg.main_feed_id)] = "MAIN"
                    if cfg.test_feed_id:
                        feeds[int(cfg.test_feed_id)] = "TEST"
        except Exception:
            logger.exception("Failed to refresh root feeds from database; keeping the previous feeds.")
            return self.root_feed_ids

        accepted: dict[int, str] = {}
        for chat_id, env in feeds.items():
            if env not in self.environments:
                continue
            if not join_policy.is_channel_id(chat_id):
                self._report_feed_problem(chat_id, "is not a channel ID (channel IDs start with -100)")
                continue
            accepted[chat_id] = env
        for chat_id in set(self._feed_verdicts) - set(accepted):
            del self._feed_verdicts[chat_id]
        self.root_feed_ids.clear()
        self.root_feed_ids.update(accepted)
        return self.root_feed_ids

    async def get_root_feed_ids(self) -> dict[str, int]:
        """Return dict of configured root feeds e.g. {'MAIN': ..., 'TEST': ...}."""
        if not self.root_feed_ids:
            await self.refresh_root_feeds()
        out: dict[str, int] = {}
        for cid, env in self.root_feed_ids.items():
            out[env] = cid
        return out

    def is_monitored(self, chat_id: int) -> bool:
        return chat_id in self.root_feed_ids or chat_id in self.active_subchannel_ids

    def context_for(self, chat_id: int) -> str:
        """Dispatcher context label for a chat, matching the live routing."""
        env = self.root_feed_ids.get(chat_id)
        if env is not None:
            return "test_feed" if env == "TEST" else "main_feed"
        return "subchannel"

    async def refresh_monitored(self, initial: bool = False) -> set[int]:
        """Re-read the root feeds and the ingestible subchannels from the database.

        Chats that stop being ingestible (IGNORED, deleted, other environment)
        are dropped, including any messages still buffered for them. Returns the
        chats that became monitored since the previous refresh (always empty on
        the initial call), so the caller can catch up on them (invariant 4).
        """
        previous = set(self.root_feed_ids) | self.active_subchannel_ids
        await self.refresh_root_feeds()
        try:
            async with database.AsyncSessionLocal() as db:
                subchannels = await crud.get_ingestible_subchannel_ids(db, self.environments)
        except Exception:
            logger.exception("Failed to refresh monitored subchannels; keeping the previous set.")
            return set()
        self.active_subchannel_ids = subchannels - set(self.root_feed_ids)
        current = set(self.root_feed_ids) | self.active_subchannel_ids

        for chat_id in sorted(previous - current):
            dropped = await self._debouncer.discard(chat_id)
            self._admins.pop(chat_id, None)
            logger.info(
                "Stopped monitoring chat_id=%s (%d buffered message(s) discarded).",
                chat_id, dropped,
            )

        if initial:
            logger.info(
                "Monitoring %d root feed(s) and %d subchannel(s) for environment(s) %s.",
                len(self.root_feed_ids), len(self.active_subchannel_ids),
                ",".join(sorted(self.environments)),
            )
            return set()
        added = current - previous
        if added:
            logger.info("Started monitoring %d chat(s): %s", len(added), sorted(added))
        return added

    # ------------------------------------------------------------------
    # Routing & Two-Tier Ingestion
    # ------------------------------------------------------------------

    async def _process_event(self, event, is_edit: bool) -> None:
        self.last_update_at = datetime.now(timezone.utc)

        # Safety guardrail: Ignore private 1-on-1 chats and direct messages
        if getattr(event, "is_private", False):
            logger.debug("Feed isolation: Dropped private chat message from chat_id=%s", event.chat_id)
            return

        if not self.is_monitored(event.chat_id):
            logger.debug(
                "Feed isolation: Dropped message from unmonitored chat_id=%s",
                event.chat_id,
            )
            return

        if is_edit and not await self._is_new_version(event.chat_id, event.message):
            return
        await self._route(event, is_edit)

    async def _route_message(self, chat_id: int, message, is_edit: bool = False) -> None:
        """Route one fetched message (catch-up, retries) through the live tier logic."""
        event = SimpleNamespace(
            chat_id=chat_id,
            message=message,
            chat=getattr(message, "chat", None),
            is_private=False,
        )
        if self.is_monitored(chat_id):
            await self._route(event, is_edit)

    async def _route(self, event, is_edit: bool) -> None:
        chat_id = event.chat_id
        # ── TIER 1: Root Feed Ingestion (main_feed_id & test_feed_id) ────────
        if chat_id in self.root_feed_ids:
            if await self._is_broadcast_feed(chat_id, event):
                await self._handle_root_feed(event, is_edit, self.root_feed_ids[chat_id])
        # ── TIER 2: Sub-Channel Ingestion (active_subchannel_ids) ─────────────
        elif chat_id in self.active_subchannel_ids:
            await self._handle_subchannel(event, is_edit)

    async def _is_new_version(self, chat_id: int, message) -> bool:
        """Whether an edit update carries new text. Telegram also sends edit
        updates when only metadata changed (e.g. a reaction); those — and
        updates for old messages that were never processed here — are ignored."""
        edit_date = _as_utc(getattr(message, "edit_date", None))
        if edit_date is None:
            return False  # never edited: a metadata-only update
        try:
            recorded = await ledger.recorded_hashes(chat_id, [message.id])
        except Exception:
            logger.exception("Ledger lookup failed for chat_id=%s; treating the edit as new.", chat_id)
            return True
        if message.id in recorded:
            stored = recorded[message.id]
            return stored is None or stored != content_hash(_message_text(message))
        if message.id in self._debouncer.pending_ids(chat_id) | self._inflight.get(chat_id, set()):
            return True
        return edit_date >= datetime.now(timezone.utc) - _BOOTSTRAP_WINDOW

    async def _is_broadcast_feed(self, chat_id: int, event) -> bool:
        """Root feeds must be broadcast channels — only their admins can post,
        so nobody else can make the worker join a chat. Checked once per feed,
        from the chat entity the update came with."""
        verdict = self._feed_verdicts.get(chat_id)
        if verdict is not None:
            return verdict
        chat = getattr(event, "chat", None)
        if chat is None:
            try:
                chat = await self._client.get_entity(chat_id)
            except Exception as exc:
                logger.warning(
                    "Root feed %s could not be checked yet (%s); its message waits for the next catch-up.",
                    chat_id, _error_text(exc),
                )
                return False
        verdict = join_policy.is_broadcast_channel(chat)
        self._feed_verdicts[chat_id] = verdict
        if not verdict:
            self._report_feed_problem(chat_id, "is not a broadcast channel (a group, person or bot)")
        return verdict

    async def _handle_root_feed(self, event, is_edit: bool, parent_env: str) -> None:
        """Tier 1 workflow: an invite post → join its channels and extract it with
        its company; any other post → debounce and extract, attributing a reply
        to an invite post to that post's company."""
        message = event.message
        chat_id = event.chat_id
        text = _message_text(message)
        if pre_filter.skip_reason(text):
            await self._skip(chat_id, message, pre_filter.EMPTY)
            return

        invite_links = join_policy.find_invite_links(text)
        if invite_links:
            logger.info(
                "Found %d invite link(s) in root feed %s (message %s)",
                len(invite_links), chat_id, message.id,
            )
            self._inflight.setdefault(chat_id, set()).add(message.id)
            self._track_task(
                asyncio.create_task(
                    self._handle_invite_post(chat_id, message, invite_links, parent_env, is_edit),
                    name=f"invite-post-{message.id}",
                )
            )
            return

        # Direct announcement: debounce and dispatch to Groq
        company_id: Optional[int] = None
        parent_context: Optional[str] = None
        reply_to_id = getattr(message, "reply_to_msg_id", None)
        if reply_to_id:
            known = await self._lookup_company(crud.get_company_by_main_feed_post, parent_env, reply_to_id)
            if known is not None:
                company_id, ingestible, _ = known
                if not ingestible:
                    await self._skip(chat_id, message, COMPANY_IGNORED)
                    return
            parent_context = await self._fetch_parent_context(chat_id, reply_to_id)

        await self._buffer(
            chat_id, _build_message_dict(message, is_edit, parent_context, company_id=company_id)
        )

    async def _handle_subchannel(self, event, is_edit: bool) -> None:
        """Tier 2 workflow: every message with text is debounced and extracted;
        in a supergroup only its admins' posts count."""
        message = event.message
        chat_id = event.chat_id
        if pre_filter.skip_reason(_message_text(message)):
            await self._skip(chat_id, message, pre_filter.EMPTY)
            return
        if not await self._is_admin_post(chat_id, event):
            await self._skip(chat_id, message, NON_ADMIN_SENDER)
            return
        await self._buffer(chat_id, _build_message_dict(message, is_edit))

    async def _buffer(self, chat_id: int, msg_dict: dict) -> None:
        await self._debouncer.add_message(
            chat_id=chat_id,
            message_dict=msg_dict,
            callback=self._debounce_callback,
            delay=settings.DEBOUNCE_DELAY_SECONDS,
            max_wait=settings.DEBOUNCE_MAX_WAIT_SECONDS,
        )

    async def _skip(self, chat_id: int, message, reason: str) -> None:
        logger.debug("Skipped message id=%s in chat=%s: %s", message.id, chat_id, reason)
        await ledger.safely(
            ledger.record, chat_id, [message.id], ledger.SKIPPED, reason,
            content_hashes={message.id: content_hash(_message_text(message))},
        )

    async def _is_admin_post(self, chat_id: int, event) -> bool:
        """Broadcast-channel posts always count. In a supergroup only posts by
        its admins do — including anonymous admins, who post as the group."""
        message = event.message
        if getattr(message, "post", False):
            return True
        sender = getattr(message, "from_id", None)
        if sender is None:
            return True  # no sender recorded: keep it (recall first)
        if isinstance(sender, types.PeerChannel):
            return utils.get_peer_id(sender) == chat_id  # others posting "as a channel" are members
        if isinstance(sender, types.PeerUser):
            admins = await self._admin_ids(chat_id, event)
            return admins is None or sender.user_id in admins
        return True

    async def _admin_ids(self, chat_id: int, event) -> Optional[frozenset]:
        """Admin user ids of a supergroup, cached for _ADMIN_CACHE_TTL. None when
        the list can't be read — its messages are then kept (recall first)."""
        now = datetime.now(timezone.utc)
        cached = self._admins.get(chat_id)
        if cached is not None and cached[1] > now:
            return cached[0]
        try:
            entity = getattr(event, "chat", None) or await self._client.get_input_entity(chat_id)
            admins = await self._client.get_participants(
                entity, filter=types.ChannelParticipantsAdmins()
            )
            ids = frozenset(user.id for user in admins)
            self._admins[chat_id] = (ids, now + _ADMIN_CACHE_TTL)
            return ids
        except Exception as exc:
            logger.warning(
                "Could not read the admins of chat_id=%s (%s); keeping its messages for now.",
                chat_id, _error_text(exc),
            )
            self._admins[chat_id] = (None, now + _ADMIN_RETRY_TTL)
            return None

    # ------------------------------------------------------------------
    # Invite posts: joining and linking subchannels
    # ------------------------------------------------------------------

    async def _handle_invite_post(
        self,
        chat_id: int,
        message,
        links: List[join_policy.InviteLink],
        parent_env: str,
        is_edit: bool,
    ) -> None:
        """Join (under the join policy) the channels an invite post links to,
        then extract the post with its company. Runs as a background task:
        joins are paced and may wait out a flood limit."""
        try:
            try:
                targets = await self._resolve_invite_links(links, parent_env)
            except _RetryLater as exc:
                await self._retry_invite_post_later(chat_id, message, exc.reason, exc.wait)
                return
            await self._extract_invite_post(chat_id, message, targets, parent_env, is_edit)
        finally:
            self._inflight.get(chat_id, set()).discard(message.id)

    async def _retry_invite_post_later(
        self, chat_id: int, message, reason: str, wait: Optional[timedelta] = None
    ) -> None:
        self.last_error = reason[:_ERROR_TEXT_LIMIT]
        logger.warning("Invite post %s in chat %s will be retried: %s", message.id, chat_id, reason)
        await ledger.safely(
            ledger.record_failure, chat_id, [message.id], reason,
            not_before=wait, content_hashes={message.id: content_hash(_message_text(message))},
        )

    async def _resolve_invite_links(
        self, links: List[join_policy.InviteLink], parent_env: str
    ) -> List[_InviteTarget]:
        """Look each link up, joining its channel when the invite is new. Links
        the join policy refuses are left out. Raises _RetryLater."""
        targets: List[_InviteTarget] = []
        seen = set()
        for link in links:
            known = await self._lookup_company(crud.get_company_by_invite_token, link.token, parent_env)
            if known is not None:
                target = _InviteTarget(link, company_id=known[0], ingestible=known[1])
                key: Any = ("company", target.company_id)
            else:
                chat = await self._join(link)
                if chat is None:
                    continue
                target = _InviteTarget(link, chat=chat)
                linked = await self._lookup_company(crud.get_company_by_subchannel_id, utils.get_peer_id(chat))
                if linked is not None:
                    if linked[2] != parent_env:
                        logger.warning(
                            "%s leads to a channel of a %s company; a %s post is not attributed to it.",
                            link.masked(), linked[2], parent_env,
                        )
                        continue
                    target.company_id, target.ingestible = linked[0], linked[1]
                key = ("company", target.company_id) if linked else ("chat", utils.get_peer_id(chat))
            if key not in seen:
                seen.add(key)
                targets.append(target)
        return targets

    async def _extract_invite_post(
        self,
        chat_id: int,
        message,
        targets: List[_InviteTarget],
        parent_env: str,
        is_edit: bool,
    ) -> None:
        source = MessageSource(chat_id, self.context_for(chat_id))
        post = _build_message_dict(message, is_edit)

        if len(targets) == 1:
            target = targets[0]
            if target.company_id is not None:
                # A re-post, or a new link to a channel that is already linked.
                if not target.ingestible:
                    await self._skip(chat_id, message, COMPANY_IGNORED)
                    return
                await self._remember_invite_post(target, message.id)
                await self._emit(replace(source, company_id=target.company_id), [post])
                return
            link = SubchannelLink(
                chat_id=utils.get_peer_id(target.chat),
                title=_chat_title(target.chat),
                invite_token=target.link.token,
                environment=parent_env,
                main_feed_msg_id=message.id,
            )
            if await self._emit(replace(source, subchannel=link), [post]):
                await self._monitor_linked_channel(link.chat_id)
            return

        # No usable link, or links to several channels: link each new channel
        # under its title, then extract the post as a plain announcement.
        for target in targets:
            if target.chat is not None and target.company_id is None:
                if not await self._link_by_title(target, parent_env):
                    await self._retry_invite_post_later(chat_id, message, "Could not link a joined channel")
                    return
        await self._emit(source, [post])

    async def _join(self, link: join_policy.InviteLink):
        """The channel *link* leads to, joined if needed — or None when the join
        policy refuses it. Links are handled one per _JOIN_INTERVAL_SECONDS, and
        none while a flood wait lasts. Raises _RetryLater."""
        async with self._join_lock:
            now = datetime.now(timezone.utc)
            if self._joins_blocked_until is not None and now < self._joins_blocked_until:
                raise _RetryLater(
                    "Joins paused after a Telegram flood wait", self._joins_blocked_until - now
                )
            await self._pace_joins()
            try:
                if link.kind == join_policy.PRIVATE:
                    return await self._join_private(link)
                return await self._join_public(link)
            except errors.FloodWaitError as exc:
                wait = timedelta(seconds=exc.seconds)
                self._joins_blocked_until = datetime.now(timezone.utc) + wait
                raise _RetryLater(f"FloodWaitError: wait {exc.seconds} s", wait) from exc
            except _PERMANENT_JOIN_ERRORS as exc:
                logger.warning("Not joining %s: %s", link.masked(), type(exc).__name__)
                return None
            except Exception as exc:  # network errors, other RPC errors: retry with backoff
                raise _RetryLater(_error_text(exc)) from exc

    async def _pace_joins(self) -> None:
        loop = asyncio.get_running_loop()
        if self._last_join_at is not None:
            delay = self._last_join_at + _JOIN_INTERVAL_SECONDS - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
        self._last_join_at = loop.time()

    async def _join_private(self, link: join_policy.InviteLink):
        """Preview the invite first (messages.checkChatInvite does not join), and
        import it only when it leads to a channel that needs no approval."""
        invite = await self._client(functions.messages.CheckChatInviteRequest(link.token))
        refusal = join_policy.check_invite(invite, self.root_feed_ids)
        if refusal:
            logger.warning("Not joining %s: %s", link.masked(), refusal)
            return None
        if isinstance(invite, types.ChatInviteAlready):
            return invite.chat
        try:
            updates = await self._client(functions.messages.ImportChatInviteRequest(link.token))
            chats = getattr(updates, "chats", None) or []
        except errors.UserAlreadyParticipantError:
            invite = await self._client(functions.messages.CheckChatInviteRequest(link.token))
            chats = [getattr(invite, "chat", None)]
        chat = next((c for c in chats if isinstance(c, types.Channel)), None)
        refusal = join_policy.check_chat(chat, self.root_feed_ids)
        if refusal:
            logger.warning("Joined %s, but it is not a usable channel (%s); not reading it.", link.masked(), refusal)
            return None
        logger.info("Joined channel %r from %s.", _chat_title(chat), link.masked())
        return chat

    async def _join_public(self, link: join_policy.InviteLink):
        """Resolve the username without joining, and join only a channel —
        never a person or a bot."""
        resolved = await self._client(functions.contacts.ResolveUsernameRequest(username=link.token))
        chat = _resolved_entity(resolved)
        refusal = join_policy.check_chat(chat, self.root_feed_ids)
        if refusal:
            logger.warning("Not joining %s: %s", link.masked(), refusal)
            return None
        if getattr(chat, "left", True):
            await self._client(functions.channels.JoinChannelRequest(chat))
            logger.info("Joined public channel %r (%s).", _chat_title(chat), link.masked())
        return chat

    async def _lookup_company(self, query, *args) -> Optional[Tuple[int, bool, str]]:
        """(company id, ingestible, environment) of the company *query* finds, or
        None. On a database error the message is processed without that
        attribution."""
        try:
            async with database.AsyncSessionLocal() as db:
                company = await query(db, *args)
                if company is None:
                    return None
                ingestible = await crud.is_company_ingestible(db, company.id, self.environments)
                return company.id, ingestible, company.environment_type
        except Exception:
            logger.exception("Company lookup (%s) failed; continuing without it.", getattr(query, "__name__", query))
            return None

    async def _remember_invite_post(self, target: _InviteTarget, post_id: int) -> None:
        try:
            async with database.AsyncSessionLocal() as db:
                await crud.record_invite_post(db, target.company_id, target.link.token, post_id)
        except Exception:
            logger.exception("Could not record invite post %s for company id=%s.", post_id, target.company_id)

    async def _link_by_title(self, target: _InviteTarget, parent_env: str) -> bool:
        """Link a channel from a several-link post under its own title. The post
        is about several companies, so replies to it are not attributed."""
        subchannel_id = utils.get_peer_id(target.chat)
        try:
            async with database.AsyncSessionLocal() as db:
                company, newly_linked = await crud.link_or_create_subchannel_company(
                    db,
                    subchannel_id=subchannel_id,
                    company_name=_chat_title(target.chat),
                    environment_type=parent_env,
                    invite_token=target.link.token,
                )
                ingestible = await crud.is_company_ingestible(db, company.id, self.environments)
        except Exception:
            logger.exception("Failed to link subchannel_id=%s to a company.", subchannel_id)
            return False
        logger.info(
            "Linked subchannel_id=%s to company=%s (env=%s, ingestible=%s)",
            subchannel_id, company.name, parent_env, ingestible,
        )
        if ingestible and newly_linked:
            self._start_backfill(subchannel_id)
        return True

    async def _monitor_linked_channel(self, subchannel_id: int) -> None:
        """Start reading a channel its invite post just linked — if its company
        may be ingested (invariants 2–3) — with the bounded 24 h backfill."""
        linked = await self._lookup_company(crud.get_company_by_subchannel_id, subchannel_id)
        if linked is None:
            logger.error("Subchannel_id=%s was not linked to a company; it is not read.", subchannel_id)
            return
        if not linked[1]:
            logger.info(
                "Not monitoring subchannel_id=%s: its company is IGNORED, deleted or outside %s.",
                subchannel_id, ",".join(sorted(self.environments)),
            )
            return
        self._start_backfill(subchannel_id)

    def _start_backfill(self, subchannel_id: int) -> None:
        """Monitor a newly linked channel and read its last 24 hours (MASTER_SPEC §2)."""
        self.active_subchannel_ids.add(subchannel_id)
        self._track_task(
            asyncio.create_task(
                self.catch_up([subchannel_id], max_age=_BOOTSTRAP_WINDOW),
                name=f"backfill-{subchannel_id}",
            )
        )

    # ------------------------------------------------------------------
    # Catch-up & retries (driven by the worker's loops)
    # ------------------------------------------------------------------

    async def _classify(self, chat_id: int, messages) -> Tuple[set, set]:
        """Split fetched messages into (new, edited) ids. New: no recorded
        outcome. Edited: handled before but edited since — its text no longer
        matches. Messages buffered or being dispatched are neither. If the ledger
        can't be read, everything counts as new (a message may be processed
        twice, never lost)."""
        busy = self._debouncer.pending_ids(chat_id) | self._inflight.get(chat_id, set())
        candidates = [m for m in messages if m.id not in busy]
        try:
            recorded = await ledger.recorded_hashes(chat_id, [m.id for m in candidates])
        except Exception:
            logger.exception("Ledger lookup failed for chat_id=%s; treating messages as unprocessed.", chat_id)
            recorded = {}
        new, edited = set(), set()
        for message in candidates:
            if message.id not in recorded:
                new.add(message.id)
                continue
            stored = recorded[message.id]
            if (
                stored is not None
                and getattr(message, "edit_date", None) is not None
                and stored != content_hash(_message_text(message))
            ):
                edited.add(message.id)
        return new, edited

    async def catch_up(
        self,
        chat_ids: Optional[Iterable[int]] = None,
        max_age: Optional[timedelta] = None,
    ) -> int:
        """Fetch and route messages missed while offline or disconnected, and
        edits made to messages already handled.

        Covers every monitored chat, or just *chat_ids*. Each chat is re-scanned
        from shortly before its ledger cursor, bounded by CATCHUP_MAX_AGE_HOURS —
        or 24 h for a chat with no history, or *max_age* when given (e.g. after an
        un-ignore). Returns the number of messages routed.
        """
        targets = (
            list(chat_ids)
            if chat_ids is not None
            else [*self.root_feed_ids, *sorted(self.active_subchannel_ids)]
        )
        routed = 0
        for chat_id in targets:
            if not self.is_monitored(chat_id) or self._feed_verdicts.get(chat_id) is False:
                continue  # ignored or removed since the catch-up was scheduled, or a refused feed
            try:
                routed += await self._catch_up_chat(chat_id, max_age)
            except Exception as exc:
                logger.warning("Catch-up failed for chat_id=%s: %s", chat_id, _error_text(exc))
            await asyncio.sleep(_CHAT_PACING_SECONDS)
        self.last_catchup_at = datetime.now(timezone.utc)
        return routed

    async def _catch_up_chat(self, chat_id: int, max_age: Optional[timedelta]) -> int:
        try:
            entity = await self._client.get_input_entity(chat_id)
        except ValueError:
            # Not in the session's entity cache yet (e.g. a fresh session). The
            # first live update from this chat caches it for the next round.
            logger.info("Catch-up skipped for chat_id=%s: not in the session cache yet.", chat_id)
            return 0

        last_seen = await ledger.cursor(chat_id)
        if max_age is not None:
            window = max_age
        elif last_seen is None:
            window = _BOOTSTRAP_WINDOW
        else:
            window = timedelta(hours=settings.CATCHUP_MAX_AGE_HOURS)
        min_id = max(0, (last_seen or 0) - _CATCHUP_LOOKBACK_MESSAGES)

        messages = await _backfill.fetch_messages_since(
            self._client, entity, min_id=min_id, max_age=window
        )
        new, edited = await self._classify(chat_id, messages)
        for message in messages:
            if message.id in new or message.id in edited:
                await self._route_message(chat_id, message, is_edit=message.id in edited)
        routed = len(new) + len(edited)
        if routed:
            logger.info(
                "Catch-up routed %d new and %d edited message(s) for chat_id=%s (cursor %s, window %s).",
                len(new), len(edited), chat_id, last_seen, window,
            )
        return routed

    async def retry_failed(self) -> int:
        """Re-route FAILED messages whose backoff has elapsed. Chats that are no
        longer monitored are left alone. Returns how many messages were retried."""
        try:
            due = await ledger.due_retries()
        except Exception:
            logger.exception("Could not read due retries from the ledger.")
            return 0
        retried = 0
        for chat_id, message_ids in due.items():
            if not self.is_monitored(chat_id):
                continue
            try:
                retried += await self._retry_chat(chat_id, message_ids)
            except Exception as exc:
                logger.warning("Retry failed for chat_id=%s: %s", chat_id, _error_text(exc))
            await asyncio.sleep(_CHAT_PACING_SECONDS)
        return retried

    async def _retry_chat(self, chat_id: int, message_ids: List[int]) -> int:
        """Fetch the messages again and route them like live ones, so a retried
        invite post re-runs its joins and every filter applies again."""
        busy = self._debouncer.pending_ids(chat_id) | self._inflight.get(chat_id, set())
        ids = sorted(set(message_ids) - busy)
        if not ids:
            return 0
        try:
            entity = await self._client.get_input_entity(chat_id)
        except ValueError:
            logger.info("Retry postponed for chat_id=%s: not in the session cache yet.", chat_id)
            return 0

        fetched = await self._client.get_messages(entity, ids=ids)
        found, deleted = [], []
        for message_id, message in zip(ids, fetched):
            if message is None:
                deleted.append(message_id)
            else:
                found.append(message)
        if deleted:
            await ledger.safely(ledger.record, chat_id, deleted, ledger.SKIPPED, MESSAGE_DELETED)
        for message in found:
            await self._route_message(chat_id, message)
        return len(found)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _fetch_parent_context(
        self, chat_id: int, reply_to_msg_id: int
    ) -> Optional[str]:
        """Retrieve the text of a parent message for context stitching."""
        try:
            parent = await self._client.get_messages(chat_id, ids=reply_to_msg_id)
            if parent:
                return _message_text(parent) or None
        except Exception:
            logger.warning(
                "Could not fetch parent message %s in chat %s",
                reply_to_msg_id,
                chat_id,
            )
        return None

    async def _debounce_callback(self, chat_id: int, messages: List[dict]) -> None:
        """Flush one chat's buffer: one batch per company (replies to an invite
        post carry its company), dropped if the chat stopped being monitored."""
        if not self.is_monitored(chat_id):
            logger.info(
                "Dropped %d buffered message(s) from chat_id=%s: no longer monitored.",
                len(messages), chat_id,
            )
            return
        context = self.context_for(chat_id)
        for company_id, group in _group_by_company(messages):
            await self._emit(MessageSource(chat_id, context, company_id=company_id), group)

    async def _emit(self, source: MessageSource, messages: List[dict]) -> bool:
        """Dispatch *messages* in batches of at most _MAX_BATCH_MESSAGES (one LLM
        call each). True if every batch was handled."""
        ok = True
        for start in range(0, len(messages), _MAX_BATCH_MESSAGES):
            ok = await self._emit_batch(source, messages[start:start + _MAX_BATCH_MESSAGES]) and ok
        return ok

    async def _emit_batch(self, source: MessageSource, messages: List[dict]) -> bool:
        """Dispatch one batch and record its outcome in the ledger: DONE on
        success, FAILED (with retry backoff) if the payload handler raises."""
        if self._payload_handler is None:
            return False
        batch = [{**m, "label": f"[{index}]"} for index, m in enumerate(messages, start=1)]
        message_ids = [m["id"] for m in batch if m.get("id") is not None]
        hashes = {m["id"]: m["content_hash"] for m in batch if m.get("id") is not None and m.get("content_hash")}
        inflight = self._inflight.setdefault(source.chat_id, set())
        inflight.update(message_ids)
        try:
            await self._payload_handler(source, batch)
        except Exception as exc:
            logger.exception(
                "Payload handler raised for context=%s chat_id=%s", source.context, source.chat_id
            )
            self.last_error = _error_text(exc)
            await ledger.safely(
                ledger.record_failure, source.chat_id, message_ids, self.last_error, content_hashes=hashes
            )
            return False
        else:
            self.last_dispatch_ok_at = datetime.now(timezone.utc)
            await ledger.safely(ledger.record, source.chat_id, message_ids, ledger.DONE, content_hashes=hashes)
            return True
        finally:
            inflight.difference_update(message_ids)
