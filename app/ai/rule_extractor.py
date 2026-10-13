"""
app/ai/rule_extractor.py
Deterministic rule-based and regex extractor for Indian Standard Time (IST) recruitment announcements.
Acts as a high-reliability fallback when the LLM outputs 0 actions for messages containing
unambiguous relative timings (e.g. "tomorrow 9pm", "today by 11:59 PM").
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from app.ai.schemas import ExtractedEvent, LLMExtractionResult

# IST is UTC+05:30
IST = timezone(timedelta(hours=5, minutes=30))

# Canonical event type regex patterns
EVENT_PATTERNS = [
    ("PPT", re.compile(r"\b(?:ppt|pre[\s-]placement\s+talk|presentation)\b", re.IGNORECASE)),
    ("OA_WINDOW", re.compile(r"\b(?:oa|online\s+assessment|coding\s+(?:test|round)|hackerrank|hackerearth)\b", re.IGNORECASE)),
    ("FORM_SUBMISSION", re.compile(r"\b(?:form|registration\s+link|google\s+form|forms\.gle)\b", re.IGNORECASE)),
    ("IRIS_DEADLINE", re.compile(r"\b(?:iris|portal)\b", re.IGNORECASE)),
    ("INTERVIEW", re.compile(r"\b(?:interview|gd|group\s+discussion|technical\s+round)\b", re.IGNORECASE)),
]

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _detect_event_type(text: str) -> str:
    for etype, pattern in EVENT_PATTERNS:
        if pattern.search(text):
            return etype
    return "CUSTOM"


def _detect_action(text: str) -> str:
    if re.search(r"\b(?:reschedule|rescheduled|postpone|postponed)\b", text, re.IGNORECASE):
        return "RESCHEDULE_EVENT"
    if re.search(r"\b(?:extend|extended|extension|pushed)\b", text, re.IGNORECASE):
        return "EXTEND_DEADLINE"
    return "CREATE_EVENT"


def _parse_ist_relative_times(text: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Parse relative dates and times in IST (+05:30) and return (start_time, end_time) in UTC.
    Matches phrases such as:
      - 'tomorrow 9pm'
      - 'tomorrow 2pm to 4pm'
      - 'today by 11:59 PM'
      - 'tonight 7 PM'
      - 'tom 17:00'
    """
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(IST)

    # 1. Determine day offset
    day_match = re.search(r"\b(today|tonight|tomorrow|tom|tmrw)\b", text, re.IGNORECASE)
    day_offset = 0
    is_tonight = False
    if day_match:
        dw = day_match.group(1).lower()
        if dw in ("tomorrow", "tom", "tmrw"):
            day_offset = 1
        elif dw == "tonight":
            is_tonight = True
            day_offset = 0
        else:
            day_offset = 0

    target_date = (now_ist + timedelta(days=day_offset)).date()

    def _to_24h(h: int, m: int, mer: Optional[str]) -> Tuple[int, int]:
        if mer:
            mer = mer.lower().replace(".", "")
            if mer == "pm" and h < 12:
                h += 12
            elif mer == "am" and h == 12:
                h = 0
        elif is_tonight and h < 12:
            h += 12
        return h, m

    # 2. Check for time range: e.g. "2pm to 4pm" or "10:00 AM - 12:00 PM"
    range_match = re.search(
        r"\b(?P<h1>\d{1,2})(?::(?P<m1>\d{2}))?\s*(?P<mer1>[ap]\.?m\.?)?\s*(?:to|-)\s*(?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?\s*(?P<mer2>[ap]\.?m\.?)?\b",
        text,
        re.IGNORECASE,
    )

    if range_match:
        h1 = int(range_match.group("h1"))
        m1 = int(range_match.group("m1")) if range_match.group("m1") else 0
        mer1 = range_match.group("mer1")

        h2 = int(range_match.group("h2"))
        m2 = int(range_match.group("m2")) if range_match.group("m2") else 0
        mer2 = range_match.group("mer2")

        if not mer1 and mer2:
            mer1 = mer2

        h1, m1 = _to_24h(h1, m1, mer1)
        h2, m2 = _to_24h(h2, m2, mer2)

        dt1_ist = datetime(
            target_date.year, target_date.month, target_date.day,
            h1, m1, 0, tzinfo=IST
        )
        dt2_ist = datetime(
            target_date.year, target_date.month, target_date.day,
            h2, m2, 0, tzinfo=IST
        )
        return dt1_ist.astimezone(timezone.utc), dt2_ist.astimezone(timezone.utc)

    # 3. Check for single time
    time_match = re.search(
        r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>[ap]\.?m\.?)?\b",
        text,
        re.IGNORECASE,
    )
    if not time_match:
        return None, None

    if not day_match and not time_match.group("meridiem") and not time_match.group("minute"):
        return None, None

    h = int(time_match.group("hour"))
    m = int(time_match.group("minute")) if time_match.group("minute") else 0
    mer = time_match.group("meridiem")

    if not mer and h > 23:
        return None, None
    if h > 24 or m > 59:
        return None, None

    h, m = _to_24h(h, m, mer)

    dt_ist = datetime(
        target_date.year, target_date.month, target_date.day,
        h, m, 0, tzinfo=IST
    )
    dt_utc = dt_ist.astimezone(timezone.utc)
    return dt_utc, dt_utc


def extract_actions_by_rules(text: str) -> LLMExtractionResult:
    """Extract recruitment actions from text using deterministic IST regex rules."""
    if not text or not text.strip():
        return LLMExtractionResult(company_name=None, actions=[])

    start_time, end_time = _parse_ist_relative_times(text)
    if not end_time and not start_time:
        return LLMExtractionResult(company_name=None, actions=[])

    event_type = _detect_event_type(text)
    action = _detect_action(text)

    # Extract action link if present
    url_match = URL_PATTERN.search(text)
    action_link = url_match.group(0).rstrip(".,;") if url_match else None

    # Derive human-readable title
    title = event_type.replace("_", " ").title()

    event = ExtractedEvent(
        action=action,
        company_name=None,
        role_name=None,
        drive_type="FTE",
        audience_scope="COHORT",
        event_type=event_type,
        title=title,
        action_link=action_link,
        start_time=start_time,
        end_time=end_time,
    )

    return LLMExtractionResult(company_name=None, actions=[event])
