"""
app/telegram/pre_filter.py
The only content gate before extraction: a message is skipped only when it has
no visible text at all.

Every other message — however short ("Slot 1", "Room 204", "Link active") —
goes on to the debouncer and the LLM. Placement channels are announcement-only,
so a length or keyword heuristic loses more announcements than it saves tokens
(PROJECT_STATE §0.2 gap C2). Only message text and media captions are sent for
extraction, so media without a caption counts as empty.
"""
import unicodedata
from typing import Optional

EMPTY = "empty"

# Invisible characters that str.isspace() does not cover: zero-width spaces and
# joiners, word joiner, BOM, invisible separators (Unicode category Cf) and the
# Hangul fillers sometimes used to post a "blank" message.
_FILLERS = frozenset({"ᅟ", "ᅠ", "ㅤ", "ﾠ"})


def _is_blank(char: str) -> bool:
    return char.isspace() or char in _FILLERS or unicodedata.category(char) == "Cf"


def skip_reason(text: Optional[str]) -> Optional[str]:
    """Ledger reason for skipping a message ("empty"), or None to keep it."""
    if not text or all(_is_blank(char) for char in text):
        return EMPTY
    return None
