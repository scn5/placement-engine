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
_JOIN_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/(?:\+|joinchat/)([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

# Downstream handler type alias
_PayloadHandler = Callable[[int, str, List[dict]], Awaitable[None]]


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
    """Stateful event router that bridges Telegram and the placement pipeline."""

    def __init__(self, client: TelegramClient) -> None:
        self._client = client
        self._debouncer = ChannelDebouncer()
        self._payload_handler: Optional[_PayloadHandler] = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def set_payload_handler(self, handler: _PayloadHandler) -> None:
        """Register the downstream classifier.  Called by Phase 4 on startup."""
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
    # Routing
    # ------------------------------------------------------------------

    async def _process_event(self, event, is_edit: bool) -> None:
        if event.chat_id == settings.MAIN_FEED_CHANNEL_ID:
            await self._handle_main_feed(event, is_edit)
        else:
            await self._handle_subchannel(event, is_edit)

    async def _handle_main_feed(self, event, is_edit: bool) -> None:
        message = event.message
        text: str = getattr(message, "text", "") or ""

        # ── Reply-to stitching ──
        parent_context: Optional[str] = None
        reply_to_id = getattr(message, "reply_to_msg_id", None)
        if reply_to_id:
            parent_context = await self._fetch_parent_context(event.chat_id, reply_to_id)

        # ── Subchannel join-link detection → background backfill ──
        join_hashes = _JOIN_LINK_RE.findall(text)
        for invite_hash in join_hashes:
            link = f"https://t.me/+{invite_hash}"
            asyncio.create_task(
                self._reconcile_and_backfill(link),
                name=f"backfill-{invite_hash[:8]}",
            )

        # ── Dispatch as a single-message main-feed payload ──
        msg_dict = _build_message_dict(message, is_edit, parent_context)
        await self._emit("main_feed", event.chat_id, [msg_dict])

    async def _handle_subchannel(self, event, is_edit: bool) -> None:
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

    async def _reconcile_and_backfill(self, invite_link: str) -> None:
        """Join / resolve a private channel entity and trigger a 24h backfill."""
        try:
            entity = await self._client.get_entity(invite_link)
            messages = await _backfill.fetch_24h_backfill(self._client, entity)
            if messages:
                entity_id: int = getattr(entity, "id", 0)
                await self._emit("backfill", entity_id, messages)
        except Exception:
            logger.warning("Entity reconciliation failed for link: %s", invite_link)

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
