"""
app/ai/prompts.py
System prompt builder and message-block formatting for extraction.

The prompt is rebuilt on every LLM call so it always carries:
  • The current IST time — but relative dates ("tomorrow") resolve against the
    posted time in each message's header, so catch-up and backfill batches are
    dated correctly (PROJECT_STATE §0.2 gap H2).
  • The known companies' schedules — every non-deleted event with its id — so an
    update targets the existing event instead of creating a duplicate (H3, H4).
  • The action, note-kind and confidence rules, and the output schema.

Each message in the user turn starts with a header line:
    [msg <id> · posted <weekday, date, time> IST · edited]
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

_IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def _ist(value: datetime) -> str:
    return value.astimezone(_IST).strftime("%a %d %b %Y, %H:%M IST")


def format_message_header(message: dict) -> str:
    """`[msg <id> · posted <IST> · edited]` for one message dict (parts it lacks are left out)."""
    parts = []
    if message.get("id") is not None:
        parts.append(f"msg {message['id']}")
    posted = message.get("date")
    if posted:
        parts.append(f"posted {_ist(datetime.fromtimestamp(posted, timezone.utc))}")
    if message.get("is_edit"):
        parts.append("edited")
    return f"[{' · '.join(parts)}]" if parts else ""


def format_message_block(messages: Iterable[dict]) -> str:
    """The user turn: every message with text, oldest first, under its header."""
    blocks = []
    for message in messages:
        text = (message.get("text") or "").strip()
        if not text:
            continue
        header = format_message_header(message)
        blocks.append(f"{header}\n{text}" if header else text)
    return "\n\n".join(blocks)


def _companies(context: Optional[dict]) -> List[dict]:
    if not context:
        return []
    if context.get("companies") is not None:
        return context["companies"]
    if context.get("company_name"):  # single-company shape
        return [{
            "company_id": context.get("company_id"),
            "company_name": context["company_name"],
            "roles": context.get("roles", []),
            "events": context.get("events", []),
        }]
    return []


def _event_line(event: dict) -> str:
    parts = [f"id={event.get('id')}", f"[{event.get('event_type', 'CUSTOM')}]", event.get("title") or "Untitled"]
    if event.get("role_name"):
        parts.append(f"role: {event['role_name']}")
    if event.get("start_time") and event.get("start_time") != event.get("end_time"):
        parts.append(f"starts {event['start_time']}")
    parts.append(f"deadline/ends {event.get('end_time', 'TBD')}")
    if event.get("action_link"):
        parts.append(f"link {event['action_link']}")
    parts.append(f"status {event.get('user_status', 'PENDING')}")
    if event.get("source_message_id"):
        parts.append(f"from msg {event['source_message_id']}")
    return "  • " + " — ".join(parts)


def _schedule_block(context: Optional[dict]) -> str:
    companies = _companies(context)
    source_id = (context or {}).get("company_id")
    if not companies:
        return (
            "## Known Companies & Their Current Schedule\n"
            "No tracked company is known for these messages. Name the company in company_name.\n"
        )
    sections = []
    for company in companies:
        title = f"### {company['company_name']}"
        if source_id is not None and company.get("company_id") == source_id:
            title += " — every message below is about this company"
        lines = [title]
        if company.get("roles"):
            lines.append("Roles: " + ", ".join(company["roles"]))
        events = company.get("events") or []
        if events:
            lines.extend(_event_line(e) for e in events)
        else:
            lines.append("  (no events tracked yet)")
        sections.append("\n".join(lines))
    return "## Known Companies & Their Current Schedule\n" + "\n\n".join(sections) + "\n"


def build_system_prompt(context: Optional[dict] = None) -> str:
    """Construct the full system prompt string.

    Args:
        context: Optional dict. "companies" → list of {company_id, company_name,
            roles, events}; each event has id, title, event_type, role_name,
            start_time / end_time (IST ISO-8601), action_link, user_status and
            source_message_id. "company_id" marks the company every message is
            about, when it is known. A single-company dict with "company_name"
            and "events" is also accepted.
    """
    now_str = datetime.now(_IST).strftime("%A, %d %B %Y %I:%M %p IST (%Y-%m-%d)")

    # ── Schema definition injected verbatim ──────────────────────────────────
    schema_example = json.dumps(
        {
            "company_name": "string or null",
            "actions": [
                {
                    "action": "CREATE_EVENT|EXTEND_DEADLINE|RESCHEDULE_EVENT|REPLACE_FORM|STATUS_NOTE|NO_ACTION",
                    "company_name": "string or null",
                    "role_name": "string or null",
                    "drive_type": "FTE|INTERNSHIP|COMBINED",
                    "audience_scope": "COHORT|INDIVIDUAL",
                    "event_type": "IRIS_DEADLINE|FORM_SUBMISSION|PPT|OA_WINDOW|INTERVIEW|CUSTOM or null",
                    "title": "string or null",
                    "action_link": "URL or null",
                    "start_time": "ISO-8601 with timezone (e.g. 2026-10-15T14:00:00+05:30) or null",
                    "end_time": "ISO-8601 with timezone (e.g. 2026-10-15T23:59:00+05:30) or null",
                    "status_note": "string or null",
                    "note_kind": "CRITERIA_RELAXATION|GENERAL or null",
                    "target_event_id": "integer id from the Known Schedule, or null",
                    "source_message_id": "integer id from the message header, or null",
                    "confidence": "HIGH|MEDIUM|LOW",
                }
            ],
        },
        indent=2,
    )

    return f"""CRITICAL JSON FORMATTING RULES:
1. You MUST generate ONLY a valid, single JSON object.
2. Your response MUST start immediately with the character '{{' and end with '}}'.
3. DO NOT include markdown code fences (e.g. ```json or ```).
4. DO NOT include any explanatory text, preambles, or notes.
5. If there are no actionable recruitment events or deadlines found in the text, you MUST output exactly:
   {{"company_name": null, "actions": []}}

You are an assistant that outputs valid JSON conforming to the requested schema.
You are a placement-drive tracking assistant for NITK Surathkal.
Your sole task is to extract structured placement actions from Telegram channel messages.
Current time: {now_str}. All NITK campus announcements use Indian Standard Time (IST, UTC+05:30).

## Message Format
Each message starts with a header line: [msg <id> · posted <date and time> IST], plus "· edited" when its author edited it after posting.
A "[CONTEXT: …]" line quotes the earlier message being replied to: use it to understand the reply, but extract only what the reply itself announces.

{_schedule_block(context)}
## Operational Rules

1. ONE EVENT, ONE ENTRY — always check the Known Schedule first.
   • An event that is not in the schedule yet → action="CREATE_EVENT".
   • A reminder or repeat of a scheduled event with the same timing → action="NO_ACTION".
   • A new date/time for a scheduled event: a later deadline → "EXTEND_DEADLINE"; a moved slot (new start and/or end) → "RESCHEDULE_EVENT".
   • A new form link that supersedes a scheduled form ("fill again", "new form", "use this link instead") → "REPLACE_FORM", with the new URL in action_link.
   • For EXTEND_DEADLINE, RESCHEDULE_EVENT and REPLACE_FORM, set target_event_id to the id of the scheduled event. Use only ids listed in the schedule; otherwise null.

2. EDITED MESSAGES
   • A header marked "edited" is a corrected version of a message that may already have been processed. Schedule entries marked "from msg <id>" came from that message: update them (with target_event_id) instead of creating new ones.
   • If the edit changes nothing actionable, return NO_ACTION.

3. SOURCE — set source_message_id to the id in the header of the message the action comes from.

4. COMPANY — set company_name to the company the messages are about (null if none). When a message covers several companies, set each action's company_name.

5. EVENT_TYPE must be one of:
   - IRIS_DEADLINE: IRIS registration/application deadlines or portal deadlines.
   - FORM_SUBMISSION: Google Forms, surveys, or registration links.
   - PPT: Pre-placement talks, company presentations, or info sessions.
   - OA_WINDOW: Online assessments, coding tests, or aptitude tests.
   - INTERVIEW: Interviews, technical rounds, HR rounds, or group discussions.
   - CUSTOM: Any other recruitment milestone.
   Use null for STATUS_NOTE and NO_ACTION.

6. AUDIENCE_SCOPE
   • If a single named student is addressed ("Nikhil Jain your interview is shifted to…"), set audience_scope="INDIVIDUAL".
   • All cohort-wide announcements use audience_scope="COHORT".

7. STATUS_NOTE — for non-temporal updates ("results delayed", "shortlist awaited", eligibility changes), with a concise status_note and a note_kind:
   • CRITERIA_RELAXATION — eligibility was widened: a lower CGPA cut-off, more branches or batches allowed, backlogs now allowed, or a previously excluded group may now apply.
   • GENERAL — every other status update.
   Never judge whether the student is eligible; only report what changed.

8. NO_ACTION — if nothing placement-relevant is announced.

9. CONFIDENCE
   • HIGH — the event and its date/time are stated explicitly.
   • MEDIUM — you inferred a detail (e.g. the year, or which scheduled event is meant).
   • LOW — you had to guess the date, the event or the company.

10. DATES & TIMES (CRITICAL)
   • Resolve relative dates against the POSTED time in the header of the message that contains them — never the current time: "today" = the posted date; "tonight" = 23:59 on the posted date; "tomorrow" = the day after the posted date; a weekday name = the next such day after the posted date. Only a message without a header uses the current time.
   • A date without a year is the next such date on or after the posted date.
   • Times without an explicit timezone are IST (UTC+05:30).
   • Emitted datetimes MUST be ISO-8601 strings with a timezone offset (e.g. "2026-10-15T23:59:00+05:30").

## Output Format (STRICT JSON — no markdown, no explanation)
{schema_example}

Return ONLY the JSON object above. No extra keys. No surrounding text. No code fences.
"""
