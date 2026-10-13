"""
app/telegram/listener.py
TelegramIngestionService — the central event router for the placement engine.

Responsibilities:
  • Register NewMessage + MessageEdited handlers on a Telethon client.
  • Route main-feed messages: reply-to stitching, subchannel join-link detection,
    and 24h backfill trigger.
  • Route subchannel messages: pre-filter noise, then debounce into batched payloads.
  • Expose set_payload_handler() so Phase 4 can register its classifier callback.

Payload handler signature:
    async def handler(chat_id: int, context: str, messages: List[dict]) -> None
    context values: "main_feed" | "subchannel" | "backfill"
"""
import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Optional

from telethon import TelegramClient, events

from app.config import settings
from app.telegram import backfill as _backfill
from app.telegram import pre_filter
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


class TelegramIngestionService:
    """Stateful event router implementing the Two-Tier Ingestion Workflow (MASTER_SPEC §2/§3)."""

    def __init__(self, client: TelegramClient) -> None:
        self._client = client
        self._debouncer = ChannelDebouncer()
        self._payload_handler: Optional[_PayloadHandler] = None
        self.active_subchannel_ids: set[int] = set()
        self.root_feed_ids: dict[int, str] = {}  # chat_id -> "MAIN" | "TEST"
        self._root_feeds = self.root_feed_ids  # alias for backward-compatibility
        self._background_tasks: set[asyncio.Task] = set()

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
        """Bootstrap the active subchannel dynamic monitoring pool and root feeds from SQLite."""
        await self.refresh_root_feeds()
        try:
            from app.database import AsyncSessionLocal
            from app.models import Company
            from sqlalchemy import select

            async with AsyncSessionLocal() as db:
                stmt = select(Company.subchannel_id).where(
                    Company.subchannel_id.is_not(None),
                    Company.is_active == True,
                )
                result = await db.execute(stmt)
                self.active_subchannel_ids = {
                    int(row[0]) for row in result.fetchall() if row[0] is not None
                }
            logger.info(
                "Initialized dynamic monitoring pool with %d active subchannel(s).",
                len(self.active_subchannel_ids),
            )
        except Exception:
            logger.exception("Failed to load active subchannels on startup.")

    async def set_payload_handler(self, handler: _PayloadHandler) -> None:
        """Register the downstream classifier. Called by Phase 4 on startup."""
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
    # Routing & Two-Tier Ingestion
    # ------------------------------------------------------------------

    async def refresh_root_feeds(self) -> dict[int, str]:
        """Collect configured feed channel IDs from Settings and DB SystemConfig."""
        self.root_feed_ids.clear()
        if settings.MAIN_FEED_CHANNEL_ID:
            self.root_feed_ids[int(settings.MAIN_FEED_CHANNEL_ID)] = "MAIN"
        if settings.TEST_FEED_CHANNEL_ID:
            self.root_feed_ids[int(settings.TEST_FEED_CHANNEL_ID)] = "TEST"
        try:
            from app.database import AsyncSessionLocal
            from app.models import SystemConfig
            from sqlalchemy import select

            async with AsyncSessionLocal() as db:
                res = await db.execute(select(SystemConfig))
                cfg = res.scalars().first()
                if cfg:
                    if cfg.main_feed_id:
                        self.root_feed_ids[int(cfg.main_feed_id)] = "MAIN"
                    if cfg.test_feed_id:
                        self.root_feed_ids[int(cfg.test_feed_id)] = "TEST"
        except Exception:
            logger.exception("Failed to refresh root feeds from database.")
        self._root_feeds = self.root_feed_ids
        return self.root_feed_ids

    async def get_root_feed_ids(self) -> dict[str, int]:
        """Return dict of configured root feeds e.g. {'MAIN': ..., 'TEST': ...}."""
        if not self.root_feed_ids:
            await self.refresh_root_feeds()
        out: dict[str, int] = {}
        for cid, env in self.root_feed_ids.items():
            out[env] = cid
        return out

    async def _is_known_subchannel(self, chat_id: int) -> bool:
        """Check if chat_id corresponds to an active company in the database."""
        try:
            from app.database import AsyncSessionLocal
            from app.models import Company
            from sqlalchemy import select

            async with AsyncSessionLocal() as db:
                res = await db.execute(
                    select(Company.id).where(
                        Company.subchannel_id == chat_id,
                        Company.is_active == True,
                    )
                )
                return res.scalars().first() is not None
        except Exception:
            return False

    async def _process_event(self, event, is_edit: bool) -> None:
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

        # Fallback check against SQLite for dynamically added companies
        if await self._is_known_subchannel(event.chat_id):
            self.active_subchannel_ids.add(event.chat_id)
            await self._handle_subchannel(event, is_edit)
            return

        logger.debug(
            "Feed isolation: Dropped message from unconfigured chat_id=%s",
            event.chat_id,
        )

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
        """Auto-join subchannel, resolve entity, store link in SQLite,
        add to active monitoring pool, and initiate 24h backfill."""
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

        # Store or link company in SQLite
        try:
            from app import crud
            from app.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                company = await crud.link_or_create_subchannel_company(
                    db=db,
                    subchannel_id=subchannel_id,
                    company_name=title,
                    environment_type=parent_env,
                )
                logger.info(
                    "Linked subchannel_id=%s to company=%s (env=%s, is_triaged=False)",
                    subchannel_id,
                    company.name,
                    parent_env,
                )
        except Exception:
            logger.exception("Failed to persist subchannel company link in database.")

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
        """Perform 24h backfill on a resolved subchannel entity."""
        from telethon import utils
        try:
            messages = await _backfill.fetch_24h_backfill(self._client, entity)
            if messages:
                subchannel_id: int = utils.get_peer_id(entity)
                context = "test_feed" if parent_env == "TEST" else "backfill"
                await self._emit(context, subchannel_id, messages)
        except Exception:
            logger.warning("Error running backfill for entity %s", getattr(entity, "id", None))

    async def _reconcile_and_backfill(self, invite_link: str) -> None:
        """Legacy helper maintained for backwards compatibility."""
        try:
            entity = await self._client.get_entity(invite_link)
            messages = await _backfill.fetch_24h_backfill(self._client, entity)
            if messages:
                from telethon import utils
                entity_id: int = utils.get_peer_id(entity)
                await self._emit("backfill", entity_id, messages)
        except Exception:
            logger.warning("Entity reconciliation failed for link: %s", invite_link)

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
        if self._payload_handler is None:
            return
        try:
            await self._payload_handler(chat_id, context, messages)
        except Exception:
            logger.exception(
                "Payload handler raised for context=%s chat_id=%s", context, chat_id
            )

