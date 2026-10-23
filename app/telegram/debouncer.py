"""
app/telegram/debouncer.py
Sliding-window debounce buffer per Telegram chat_id.

Accumulates rapid message bursts and emits one consolidated, chronologically
sorted payload to a downstream callback after a configurable quiet period.

  • max_wait caps how long a busy chat can keep postponing its flush.
  • A newer version of a buffered message (an edit) replaces it, so the batch
    holds each message once, in its latest form.
  • Batches of one chat never overlap: a flush waits for the previous one.

Asyncio safety notes:
- State mutations happen in the single event-loop thread.
- Task cancellation is handled with explicit try/except CancelledError.
- Unhandled exceptions inside the callback are caught and logged; they never
  propagate to the event loop's exception handler.
"""
import asyncio
import logging
from typing import Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Type alias for the per-channel buffer slot
_Slot = Dict  # {"messages": List[dict], "first_at": float, "timer_task": Optional[asyncio.Task]}

# Downstream callback signature: (chat_id, messages) -> None
_Callback = Callable[[int, List[dict]], Awaitable[None]]


class ChannelDebouncer:
    """One-instance-per-process sliding-window debounce engine."""

    def __init__(self) -> None:
        self._slots: Dict[int, _Slot] = {}
        self._locks: Dict[int, asyncio.Lock] = {}
        # Ids taken out of the buffer whose batch has not finished dispatching.
        self._queued: Dict[int, set] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add_message(
        self,
        chat_id: int,
        message_dict: dict,
        callback: _Callback,
        delay: float,
        max_wait: Optional[float] = None,
    ) -> None:
        """Buffer a message and reset the countdown for *chat_id*; with
        *max_wait*, the buffer flushes at most that long after its first message.

        Fields expected in *message_dict*:
            id, text, date (UTC timestamp float), is_edit, reply_to_msg_id
        """
        loop = asyncio.get_running_loop()
        slot = self._get_or_create(chat_id, loop.time())

        # ── Cancel the in-flight timer, if any ──
        await self._cancel_slot_timer(slot)

        # ── Accumulate ──
        self._merge(slot["messages"], message_dict)

        # ── Arm a fresh countdown ──
        wait = delay
        if max_wait is not None:
            wait = max(0.0, min(delay, slot["first_at"] + max_wait - loop.time()))
        slot["timer_task"] = asyncio.create_task(
            self._fire_after(chat_id, callback, wait),
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

    def pending_ids(self, chat_id: int) -> set:
        """Message ids of *chat_id* not dispatched yet: buffered, or in a batch
        waiting for (or still being handled by) the callback."""
        slot = self._slots.get(chat_id)
        buffered = {m.get("id") for m in slot["messages"]} if slot else set()
        return buffered | self._queued.get(chat_id, set())

    async def discard(self, chat_id: int) -> int:
        """Drop *chat_id*'s buffer without dispatching it; returns how many
        messages were discarded."""
        slot = self._slots.pop(chat_id, None)
        if not slot:
            return 0
        await self._cancel_slot_timer(slot)
        return len(slot["messages"])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create(self, chat_id: int, now: float) -> _Slot:
        if chat_id not in self._slots:
            self._slots[chat_id] = {"messages": [], "first_at": now, "timer_task": None}
        return self._slots[chat_id]

    @staticmethod
    def _merge(messages: List[dict], message: dict) -> None:
        """Append *message*, or let it replace an older version of itself. The
        replaced version was never dispatched, so the batch keeps its is_edit
        flag: an original edited before its flush is still a new message."""
        message_id = message.get("id")
        if message_id is not None:
            for index, existing in enumerate(messages):
                if existing.get("id") == message_id:
                    messages[index] = {**message, "is_edit": existing.get("is_edit", False)}
                    return
        messages.append(message)

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
        """Pop the slot, sort by date ASC, and invoke the callback once the
        chat's previous batch has finished."""
        slot = self._slots.pop(chat_id, None)
        if not slot or not slot["messages"]:
            return

        # Chronological sort on the UTC timestamp stored in "date"
        messages = sorted(slot["messages"], key=lambda m: m.get("date") or 0)
        ids = {m.get("id") for m in messages}
        queued = self._queued.setdefault(chat_id, set())
        queued.update(ids)

        try:
            async with self._locks.setdefault(chat_id, asyncio.Lock()):
                await callback(chat_id, messages)
        except Exception:
            logger.exception(
                "Debounce callback raised for chat_id=%s — catch-up will pick the messages up again",
                chat_id,
            )
        finally:
            queued.difference_update(ids)
