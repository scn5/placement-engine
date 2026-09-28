"""
app/telegram/pre_filter.py
Deterministic, regex-based noise gate for Telegram messages.

Design philosophy: fast, zero-dependency, 100% unit-testable with no Telethon imports.
"""
import re
from typing import Optional

# ---------------------------------------------------------------------------
# Recruitment signal patterns
# Any message containing at least ONE of these signals is considered actionable.
# ---------------------------------------------------------------------------
_SIGNAL_RE = re.compile(
    r"(?:"
    # Explicit time values: 10:30, 10:30 AM
    r"\d{1,2}[:/]\d{2}"
    r"|\d{1,2}\s*(?:am|pm)\b"
    # Calendar date references: "Oct 15", "15 Oct", "15/10", "October 15"
    r"|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?"
    r"|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\s+\d{1,2}"
    r"|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b"
    r"|\d{1,2}/\d{1,2}(?:/\d{2,4})?"
    # Placement-specific keywords
    r"|\bdeadline\b"
    r"|\bform\b"
    r"|\boa\b"
    r"|\bshortlist(?:ed)?\b"
    r"|\binterview\b"
    r"|https?://"
    r"|\biris\b"
    r"|\brescheduled\b"
    r"|\bextended\b"
    r"|\bppt\b"
    r"|\bstipend\b"
    r"|\bctc\b"
    r"|\bregistration\b"
    r"|\bapplication\b"
    r"|\bselection\b"
    r"|\boffline\b"
    r"|\bonline\b"
    r"|\bround\b"
    r"|\btest\b"
    r"|\blink\b"
    r")",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Administrative SPOC chatter — drop unconditionally even if signals present.
# ---------------------------------------------------------------------------
_ADMIN_CHATTER_RE = re.compile(
    r"(?:"
    # "Name -> send phone number" style SPOC routing
    r"[\w\s]+\s*->\s*send"
    r"|\bsend\s+(?:your\s+)?(?:phone\s+number|number|resume|cv|email|contact\s+details?)\b"
    # Direct message requests with no substance
    r"|\bdm\s+me\b"
    r"|\bping\s+(?:me|us)\b"
    r"|\bwhatsapp\s+me\b"
    # Pure acknowledgement noise
    r"|\bnoted\b"
    r"|\bokay\b"
    r"|\bok\b"
    r"|\bthanks?\b"
    r"|\bthank\s+you\b"
    r")",
    re.IGNORECASE,
)

# Short message length threshold — anything under 8 chars with no media is noise
_MIN_TEXT_LENGTH = 8


def should_filter_noise(text: Optional[str], has_media: bool) -> bool:
    """Return True (→ drop) if the message is noise; False if it is actionable.

    Decision tree:
    1. Media present             → keep unconditionally (JD, schedule image, etc.)
    2. Empty / whitespace text   → drop
    3. Below minimum length      → drop
    4. Admin/SPOC chatter match  → drop
    5. No recruitment signals    → drop
    6. Otherwise                 → keep
    """
    if has_media:
        return False

    if not text or not text.strip():
        return True

    stripped = text.strip()

    if len(stripped) < _MIN_TEXT_LENGTH:
        return True

    if _ADMIN_CHATTER_RE.search(stripped):
        return True

    if not _SIGNAL_RE.search(stripped):
        return True

    return False
