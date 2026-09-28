"""
app/telegram/debouncer.py
Sliding-window debounce buffer per Telegram chat_id.

Accumulates rapid message bursts and emits one consolidated, chronologically
sorted payload to a downstream callback after a configurable quiet period.

Asyncio safety notes:
- All state mutations happen in the single event-loop thread — no locks needed.
- Task cancellation is handled with explicit try/except CancelledError.
- Unhandled exceptions inside the callback are caught and logged; they never
  propagate to the event loop's exception handler.
"""
import asyncio
import logging
from typing import Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Type alias for the per-channel buffer slot
_Slot = Dict  # {"messages": List[dict], "timer_task": Optional[asyncio.Task]}

# Downstream callback signature: (chat_id, messages) -> None
_Callback = Callable[[int, List[dict]], Awaitable[None]]


class ChannelDebouncer:
    """One-instance-per-process sliding-window debounce engine."""

    def __init__(self) -> None:
        self._slots: Dict[int, _Slot] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add_message(
        self,
        chat_id: int,
        message_dict: dict,
        callback: _Callback,
        delay: float,
    ) -> None:
        """Buffer a message and reset the countdown for *chat_id*.

        Fields expected in *message_dict*:
            id, text, date (UTC timestamp float), is_edit, reply_to_msg_id
        """
        slot = self._get_or_create(chat_id)

        # ── Cancel the in-flight timer, if any ──
        await self._cancel_slot_timer(slot)

        # ── Accumulate ──
        slot["messages"].append(message_dict)

        # ── Arm a fresh countdown ──
        slot["timer_task"] = asyncio.create_task(
            self._fire_after(chat_id, callback, delay),
            name=f"debounce-{chat_id}",
        )

    async def flush(self, chat_id: int, callback: _Callback) -> None:
        """Force-flush the pending buffer for *chat_id* without waiting.

        Useful for graceful shutdown or test synchronisation.
        """
        slot = self._slots.get(chat_id)
        if not slot:
            return
        await self._cancel_slot_timer(slot)
        await self._dispatch(chat_id, callback)

    async def flush_all(self, callback: _Callback) -> None:
        """Flush every pending channel buffer (called on service shutdown)."""
        for chat_id in list(self._slots.keys()):
            await self.flush(chat_id, callback)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create(self, chat_id: int) -> _Slot:
        if chat_id not in self._slots:
            self._slots[chat_id] = {"messages": [], "timer_task": None}
        return self._slots[chat_id]

    @staticmethod
    async def _cancel_slot_timer(slot: _Slot) -> None:
        task: Optional[asyncio.Task] = slot.get("timer_task")
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        slot["timer_task"] = None

    async def _fire_after(
        self,
        chat_id: int,
        callback: _Callback,
        delay: float,
    ) -> None:
        """Sleep for *delay* seconds, then dispatch — unless cancelled first."""
        await asyncio.sleep(delay)
        await self._dispatch(chat_id, callback)

    async def _dispatch(self, chat_id: int, callback: _Callback) -> None:
        """Pop the slot, sort by date ASC, and invoke the callback."""
        slot = self._slots.pop(chat_id, None)
        if not slot or not slot["messages"]:
            return

        # Chronological sort on the UTC timestamp stored in "date"
        messages = sorted(slot["messages"], key=lambda m: m.get("date") or 0)

        try:
            await callback(chat_id, messages)
        except Exception:
            logger.exception(
                "Debounce callback raised for chat_id=%s — messages dropped silently",
                chat_id,
            )
