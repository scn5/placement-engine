"""
app/ai/prompts.py
System prompt builder with temporal and schedule-state injection.

The prompt is rebuilt on every LLM call so it always carries:
  • Current UTC timestamp  — eliminates ambiguous relative references.
  • Known company schedule — allows the LLM to resolve "same timings" / "Tuesday" etc.
  • Strict schema rules    — minimises hallucinated field names.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")


def build_system_prompt(context: Optional[dict] = None) -> str:
    """Construct the full system prompt string.

    Args:
        context: Optional dict with keys:
            "company_name"  → str
            "company_id"    → int
            "events"        → List[dict] each with id, title, event_type,
                              end_time (ISO-8601), user_status
    """
    now_ist = datetime.now(_IST)
    now_str = now_ist.strftime("%A, %d %B %Y %I:%M %p IST (%Y-%m-%d)")
    today_date = now_ist.strftime("%Y-%m-%d")
    tomorrow_date = (now_ist + timedelta(days=1)).strftime("%Y-%m-%d")

    # ── Known schedule injection ──────────────────────────────────────────────
    schedule_block = ""
    if context and context.get("company_name"):
        company_name = context["company_name"]
        events: List[dict] = context.get("events", [])

        if events:
            event_lines = "\n".join(
                f"  • [{e.get('event_type', 'EVENT')}] "
                f"{e.get('title', 'Untitled')} "
                f"— deadline {e.get('end_time', 'TBD')} "
                f"(status: {e.get('user_status', 'PENDING')})"
                for e in events
            )
            schedule_block = (
                f"\n## Known Company: {company_name}\n"
                f"### Current Schedule State (use this to resolve relative references):\n"
                f"{event_lines}\n"
            )
        else:
            schedule_block = (
                f"\n## Known Company: {company_name}\n"
                f"No events currently tracked for this company.\n"
            )

    # ── Schema definition injected verbatim ──────────────────────────────────
    schema_example = json.dumps(
        {
            "company_name": "string or null",
            "actions": [
                {
                    "action": "CREATE_EVENT|EXTEND_DEADLINE|RESCHEDULE_EVENT|REPLACE_FORM|STATUS_NOTE|NO_ACTION",
                    "role_name": "string or null",
                    "drive_type": "FTE|INTERNSHIP|COMBINED",
                    "audience_scope": "COHORT|INDIVIDUAL",
                    "event_type": "IRIS_DEADLINE|FORM_SUBMISSION|PPT|OA_WINDOW|INTERVIEW|CUSTOM",
                    "title": "string or null",
                    "action_link": "URL or null",
                    "start_time": "ISO-8601 with timezone (e.g. 2026-10-15T14:00:00+05:30) or null",
                    "end_time": "ISO-8601 with timezone (e.g. 2026-10-15T23:59:59+05:30) or null",
                    "status_note": "string or null",
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
Today is {now_str}. All NITK campus announcements use Indian Standard Time (IST, UTC+05:30).
{schedule_block}
## Operational Rules (apply in order)

1. ACTION SELECTION
   • Use action="CREATE_EVENT" whenever an event, session, or deadline is announced.
   • ONLY use action="EXTEND_DEADLINE" or action="RESCHEDULE_EVENT" if words like "extended", "postponed", "pushed", "revised", or "rescheduled" are explicitly in the text.
   • Canonical event_type MUST strictly be one of:
     - IRIS_DEADLINE: IRIS registration/application deadlines or portal deadlines.
     - FORM_SUBMISSION: Google Forms, surveys, or registration links.
     - PPT: Pre-placement talks, company presentations, or info sessions.
     - OA_WINDOW: Online assessments, coding tests, or aptitude tests.
     - INTERVIEW: Interviews, technical rounds, HR rounds, or group discussions.
     - CUSTOM: Any other recruitment milestones.

2. AUDIENCE_SCOPE
   • If a single named student is addressed ("Nikhil Jain your interview is shifted to…"),
     set audience_scope="INDIVIDUAL".
   • All cohort-wide announcements use audience_scope="COHORT".

3. REPLACE_FORM
   • If a coordinator posts a new form URL with phrasing like "fill again", "new form",
     or it clearly supersedes an existing form link, set action="REPLACE_FORM".
   • action_link must contain the new URL.

4. EXTEND_DEADLINE
   • If a deadline is "extended", "shifted to", "pushed to", or a new date/time is given
     for an existing event, set action="EXTEND_DEADLINE".
   • Resolve relative references ("same timings", "Tuesday") using the Known Schedule State.

5. RESCHEDULE_EVENT
   • Use for full slot changes (new start_time + end_time) that are not simple extensions.

6. STATUS_NOTE
   • If the update is non-temporal ("results delayed", "shortlists pending", "no update yet", or criteria relaxation like "6.5+ cgpa students can apply"),
     set action="STATUS_NOTE" with a concise human-readable status_note.

7. NO_ACTION
   • If the message contains no placement-relevant action, set action="NO_ACTION".

8. DATETIME & RELATIVE DATE RESOLUTION (CRITICAL):
   • Reference timestamp: Today is {now_str}.
   • "today" -> calendar date {today_date} IST.
   • "tonight" -> calendar date {today_date} at 23:59:59 IST.
   • "tomorrow" -> calendar date {tomorrow_date} IST.
   • Weekday names (e.g., "Monday", "Tuesday") refer to the next upcoming occurrence relative to today.
   • Times without explicit timezone are in IST (UTC+05:30).
   • Emitted datetimes MUST be ISO-8601 strings with timezone offset (e.g. "{today_date}T23:59:00+05:30" or in UTC ending with 'Z').

## Output Format (STRICT JSON — no markdown, no explanation)
{schema_example}

Return ONLY the JSON object above. No extra keys. No surrounding text. No code fences.
"""
