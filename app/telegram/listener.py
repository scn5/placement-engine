"""
app/telegram/listener.py
TelegramIngestionService — the central event router for the placement engine.

Responsibilities:
  • Register NewMessage + MessageEdited handlers on a Telethon client.
  • Route main-feed messages: reply-to stitching, subchannel join-link detection,
    and 24h backfill trigger.
  • Route subchannel messages: pre-filter noise, then debounce into batched payloads.
  • Keep the monitored chats in sync with the database: only ingested
    environments, never effectively-IGNORED or deleted companies
    (PROJECT_STATE §0.1 invariants 2–4).
  • Record every message's outcome in the ledger, catch up on messages missed
    while offline, and retry batches whose dispatch failed.
  • Expose set_payload_handler() so the worker can register the dispatcher.

Payload handler signature:
    async def handler(chat_id: int, context: str, messages: List[dict]) -> None
    context values: "main_feed" | "test_feed" | "subchannel" | "backfill"
    A handler that raises leaves the batch FAILED in the ledger (retried later).
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Awaitable, Callable, Iterable, List, Optional

from sqlalchemy import select
from telethon import TelegramClient, events

from app import crud, database
from app.config import settings
from app.models import SystemConfig
from app.telegram import backfill as _backfill
from app.telegram import ledger, pre_filter
from app.telegram.debouncer import ChannelDebouncer

logger = logging.getLogger(__name__)

# Matches private invite links embedded in messages
# Examples: t.me/+AbCdEfGh  or  t.me/joinchat/AbCdEfGh
_PRIVATE_INVITE_RE = re.compile(
    r"(?:https?://)?t\.me/(?:\+|joinchat/)([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

# Matches public channel links embedded in messages
# Example: https://t.me/company_placement_2026 or t.me/company_channel
_PUBLIC_CHANNEL_RE = re.compile(
    r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})",
    re.IGNORECASE,
)
_EXCLUDED_PUBLIC_SLUGS = {
    "joinchat", "share", "login", "setlanguage", "contact",
    "addstickers", "proxy", "socks", "c"
}

# Chats with no ledger history are bootstrapped from this window (MASTER_SPEC §2).
_BOOTSTRAP_WINDOW = timedelta(hours=24)
# Catch-up re-scans this many messages behind the cursor: a crash can leave
# buffered messages below a later SKIPPED/DONE one.
_CATCHUP_LOOKBACK_MESSAGES = 100
# Pause between chats during catch-up and retries, to stay clear of flood limits.
_CHAT_PACING_SECONDS = 1.0
_ERROR_TEXT_LIMIT = 200

# Downstream handler type alias
_PayloadHandler = Callable[[int, str, List[dict]], Awaitable[None]]


def _extract_invite_links(text: str) -> List[tuple[str, str]]:
    """Extract Telegram invite links. Returns list of (link_type, token),
    where link_type is 'private' (hash) or 'public' (username)."""
    links: List[tuple[str, str]] = []
    seen = set()
    for inv_hash in _PRIVATE_INVITE_RE.findall(text):
        if inv_hash not in seen:
            seen.add(inv_hash)
            links.append(("private", inv_hash))

    cleaned = _PRIVATE_INVITE_RE.sub("", text)
    for uname in _PUBLIC_CHANNEL_RE.findall(cleaned):
        if uname.lower() not in _EXCLUDED_PUBLIC_SLUGS and uname not in seen:
            seen.add(uname)
            links.append(("public", uname))
    return links


def _build_message_dict(
    message,
    is_edit: bool,
    parent_context: Optional[str] = None,
) -> dict:
    """Normalise a Telethon Message object into a plain dict for pipeline use."""
    raw_text: str = getattr(message, "text", "") or ""
    stitched_text = (
        f"[CONTEXT: {parent_context}]\n{raw_text}" if parent_context else raw_text
    )

    raw_date = getattr(message, "date", None)
    if raw_date is not None and raw_date.tzinfo is None:
        raw_date = raw_date.replace(tzinfo=timezone.utc)

    return {
        "id": message.id,
        "text": stitched_text,
        "raw_text": raw_text,
        "date": raw_date.timestamp() if raw_date else 0.0,
        "is_edit": is_edit,
        "reply_to_msg_id": getattr(message, "reply_to_msg_id", None),
        "parent_context": parent_context,
        "has_media": getattr(message, "media", None) is not None,
    }


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
        self._root_feeds = self.root_feed_ids  # alias for backward-compatibility
        self._background_tasks: set[asyncio.Task] = set()
        self._inflight: dict[int, set[int]] = {}  # message ids being dispatched, per chat
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
        """Attach Telethon event handlers to the client."""
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

    async def refresh_root_feeds(self) -> dict[int, str]:
        """Collect configured feed channel IDs from Settings and DB SystemConfig,
        keeping only the ingested environments. On a database error the previous
        feeds are kept, so a transient failure never stops ingestion."""
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
        self.root_feed_ids.clear()
        self.root_feed_ids.update(
            {chat_id: env for chat_id, env in feeds.items() if env in self.environments}
        )
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

        # ── TIER 1: Root Feed Ingestion (main_feed_id & test_feed_id) ────────
        if event.chat_id in self.root_feed_ids:
            parent_env = self.root_feed_ids[event.chat_id]
            await self._handle_root_feed(event, is_edit, parent_env)
            return

        # ── TIER 2: Sub-Channel Ingestion (active_subchannel_ids) ─────────────
        if event.chat_id in self.active_subchannel_ids:
            await self._handle_subchannel(event, is_edit)
            return

        logger.debug(
            "Feed isolation: Dropped message from unmonitored chat_id=%s",
            event.chat_id,
        )

    async def _route_message(self, chat_id: int, message, is_edit: bool = False) -> None:
        """Route one fetched message (catch-up) through the live tier logic."""
        event = SimpleNamespace(chat_id=chat_id, message=message, is_private=False)
        if chat_id in self.root_feed_ids:
            await self._handle_root_feed(event, is_edit, self.root_feed_ids[chat_id])
        elif chat_id in self.active_subchannel_ids:
            await self._handle_subchannel(event, is_edit)

    async def _handle_root_feed(self, event, is_edit: bool, parent_env: str) -> None:
        """Tier 1 workflow: Check invite links -> auto-join & link subchannel,
        OR route direct announcements into debounce buffer for Groq extraction."""
        message = event.message
        text: str = getattr(message, "text", "") or ""

        invite_links = _extract_invite_links(text)
        if invite_links:
            logger.info("Found %d invite link(s) in root feed %s", len(invite_links), event.chat_id)
            for link_type, token in invite_links:
                self._track_task(
                    asyncio.create_task(
                        self._join_and_link_subchannel(link_type, token, parent_env),
                        name=f"join-{token[:8]}",
                    )
                )
            # The post's own text is not extracted yet (gap C3, Phase 0.4).
            await ledger.safely(
                ledger.record, event.chat_id, [message.id], ledger.SKIPPED, "invite_post"
            )
        else:
            # Direct announcement: debounce and dispatch directly to Groq
            parent_context: Optional[str] = None
            reply_to_id = getattr(message, "reply_to_msg_id", None)
            if reply_to_id:
                parent_context = await self._fetch_parent_context(event.chat_id, reply_to_id)

            msg_dict = _build_message_dict(message, is_edit, parent_context)
            context_header = "test_feed" if parent_env == "TEST" else "main_feed"
            await self._debouncer.add_message(
                chat_id=event.chat_id,
                message_dict=msg_dict,
                callback=lambda cid, msgs: self._emit(context_header, cid, msgs),
                delay=settings.DEBOUNCE_DELAY_SECONDS,
            )

    async def _handle_main_feed(self, event, is_edit: bool) -> None:
        """Convenience method maintained for backward compatibility."""
        await self._handle_root_feed(event, is_edit, "MAIN")

    async def _handle_subchannel(self, event, is_edit: bool) -> None:
        """Tier 2 workflow: Pre-filter noise, debounce bursts, then dispatch to Groq."""
        message = event.message
        text: str = getattr(message, "text", "") or ""
        has_media: bool = getattr(message, "media", None) is not None

        if pre_filter.should_filter_noise(text, has_media):
            logger.debug(
                "Pre-filter: dropped message id=%s from chat=%s",
                message.id,
                event.chat_id,
            )
            await ledger.safely(
                ledger.record, event.chat_id, [message.id], ledger.SKIPPED, "pre_filter"
            )
            return

        msg_dict = _build_message_dict(message, is_edit)
        await self._debouncer.add_message(
            chat_id=event.chat_id,
            message_dict=msg_dict,
            callback=self._debounce_callback,
            delay=settings.DEBOUNCE_DELAY_SECONDS,
        )

    # ------------------------------------------------------------------
    # Subchannel Auto-Joining & Backfill
    # ------------------------------------------------------------------

    async def _join_and_link_subchannel(
        self, link_type: str, token: str, parent_env: str
    ) -> Optional[int]:
        """Auto-join subchannel, resolve entity, store link in the database, and —
        if the company may be ingested — add it to the monitoring pool and run
        the 24h backfill."""
        from telethon.tl.functions.messages import ImportChatInviteRequest
        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.errors import UserAlreadyParticipantError
        from telethon import utils

        entity = None
        try:
            if link_type == "private":
                invite_link = f"https://t.me/+{token}"
                try:
                    await self._client(ImportChatInviteRequest(token))
                    logger.info("ImportChatInviteRequest succeeded for private hash=%s", token)
                except UserAlreadyParticipantError:
                    logger.debug("Already participant in private invite %s", token)
                except Exception as exc:
                    logger.warning("ImportChatInviteRequest error for %s: %s", token, exc)

                try:
                    entity = await self._client.get_entity(invite_link)
                except Exception:
                    entity = await self._client.get_entity(token)
            else:
                try:
                    await self._client(JoinChannelRequest(token))
                    logger.info("JoinChannelRequest succeeded for public channel=%s", token)
                except UserAlreadyParticipantError:
                    logger.debug("Already participant in public channel %s", token)
                except Exception as exc:
                    logger.warning("JoinChannelRequest error for %s: %s", token, exc)

                entity = await self._client.get_entity(token)
        except Exception as exc:
            logger.warning("Failed to resolve channel entity for %s %s: %s", link_type, token, exc)
            return None

        if entity is None:
            return None

        subchannel_id: int = utils.get_peer_id(entity)
        title = (
            getattr(entity, "title", None)
            or getattr(entity, "username", None)
            or f"Subchannel {token}"
        )

        # Store or link the company, then apply the ingestion gate (invariants 2–3).
        try:
            async with database.AsyncSessionLocal() as db:
                company = await crud.link_or_create_subchannel_company(
                    db=db,
                    subchannel_id=subchannel_id,
                    company_name=title,
                    environment_type=parent_env,
                )
                ingestible = await crud.is_company_ingestible(db, company.id, self.environments)
                logger.info(
                    "Linked subchannel_id=%s to company=%s (env=%s, ingestible=%s)",
                    subchannel_id,
                    company.name,
                    parent_env,
                    ingestible,
                )
        except Exception:
            logger.exception("Failed to persist subchannel company link in database.")
            return None

        if not ingestible:
            logger.info(
                "Not monitoring subchannel_id=%s: its company is IGNORED, deleted or outside %s.",
                subchannel_id, ",".join(sorted(self.environments)),
            )
            return subchannel_id

        # Register in dynamic monitoring pool
        self.active_subchannel_ids.add(subchannel_id)

        # Trigger 24h backfill bootstrap
        self._track_task(
            asyncio.create_task(
                self._reconcile_and_backfill_entity(entity, parent_env),
                name=f"backfill-{subchannel_id}",
            )
        )

        return subchannel_id

    async def _reconcile_and_backfill_entity(self, entity, parent_env: str) -> None:
        """Perform 24h backfill on a resolved subchannel entity. Messages already
        processed, buffered or in flight are skipped, so a re-posted invite never
        re-processes a channel."""
        from telethon import utils
        try:
            messages = await _backfill.fetch_24h_backfill(self._client, entity)
            if not messages:
                return
            subchannel_id: int = utils.get_peer_id(entity)
            fresh = await self._unprocessed(subchannel_id, [m["id"] for m in messages])
            messages = [m for m in messages if m["id"] in fresh]
            if not messages:
                logger.info("Backfill for chat_id=%s: nothing new to process.", subchannel_id)
                return
            for index, entry in enumerate(messages, start=1):
                entry["index"] = index
                entry["label"] = f"[{index}]"
            context = "test_feed" if parent_env == "TEST" else "backfill"
            await self._emit(context, subchannel_id, messages)
        except Exception:
            logger.warning("Error running backfill for entity %s", getattr(entity, "id", None))

    # ------------------------------------------------------------------
    # Catch-up & retries (driven by the worker's loops)
    # ------------------------------------------------------------------

    async def _unprocessed(self, chat_id: int, message_ids: Iterable[int]) -> set[int]:
        """Ids with no recorded outcome that are neither buffered nor being
        dispatched. If the ledger can't be read, everything counts as
        unprocessed (a message may be processed twice, never lost)."""
        ids = set(message_ids)
        if not ids:
            return set()
        busy = self._debouncer.pending_ids(chat_id) | self._inflight.get(chat_id, set())
        try:
            known = await ledger.known_ids(chat_id, ids)
        except Exception:
            logger.exception("Ledger lookup failed for chat_id=%s; treating messages as unprocessed.", chat_id)
            known = set()
        return ids - known - busy

    async def catch_up(
        self,
        chat_ids: Optional[Iterable[int]] = None,
        max_age: Optional[timedelta] = None,
    ) -> int:
        """Fetch and route messages missed while offline or disconnected.

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
            if not self.is_monitored(chat_id):
                continue  # ignored or removed since the catch-up was scheduled
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
        fresh = await self._unprocessed(chat_id, [m.id for m in messages])
        routed = 0
        for message in messages:
            if message.id in fresh:
                await self._route_message(chat_id, message)
                routed += 1
        if routed:
            logger.info(
                "Catch-up routed %d message(s) for chat_id=%s (cursor %s, window %s).",
                routed, chat_id, last_seen, window,
            )
        return routed

    async def retry_failed(self) -> int:
        """Re-dispatch FAILED messages whose backoff has elapsed. Chats that are
        no longer monitored are left alone. Returns how many messages were retried."""
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
            await ledger.safely(ledger.record, chat_id, deleted, ledger.SKIPPED, "message_deleted")
        if not found:
            return 0

        is_root_feed = chat_id in self.root_feed_ids
        batch = []
        for message in found:
            parent_context = None
            if is_root_feed and getattr(message, "reply_to_msg_id", None):
                parent_context = await self._fetch_parent_context(chat_id, message.reply_to_msg_id)
            batch.append(_build_message_dict(message, is_edit=False, parent_context=parent_context))
        await self._emit(self.context_for(chat_id), chat_id, batch)
        return len(batch)

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
                return getattr(parent, "text", None) or None
        except Exception:
            logger.warning(
                "Could not fetch parent message %s in chat %s",
                reply_to_msg_id,
                chat_id,
            )
        return None

    async def _debounce_callback(self, chat_id: int, messages: List[dict]) -> None:
        await self._emit("subchannel", chat_id, messages)

    async def _emit(
        self, context: str, chat_id: int, messages: List[dict]
    ) -> None:
        """Dispatch one batch and record its outcome in the ledger: DONE on
        success, FAILED (with retry backoff) if the payload handler raises."""
        if self._payload_handler is None:
            return
        message_ids = [m["id"] for m in messages if m.get("id") is not None]
        inflight = self._inflight.setdefault(chat_id, set())
        inflight.update(message_ids)
        try:
            await self._payload_handler(chat_id, context, messages)
        except Exception as exc:
            logger.exception(
                "Payload handler raised for context=%s chat_id=%s", context, chat_id
            )
            self.last_error = _error_text(exc)
            await ledger.safely(ledger.record_failure, chat_id, message_ids, self.last_error)
        else:
            self.last_dispatch_ok_at = datetime.now(timezone.utc)
            await ledger.safely(ledger.record, chat_id, message_ids, ledger.DONE)
        finally:
            inflight.difference_update(message_ids)
