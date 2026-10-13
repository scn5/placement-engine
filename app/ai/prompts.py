"""
app/ai/prompts.py
System prompt builder with temporal and schedule-state injection.

The prompt is rebuilt on every LLM call so it always carries:
  • Current UTC timestamp  — eliminates ambiguous relative references.
  • Known company schedule — allows the LLM to resolve "same timings" / "Tuesday" etc.
  • Strict schema rules    — minimises hallucinated field names.
"""
import json
from datetime import datetime, timezone
from typing import List, Optional


def build_system_prompt(context: Optional[dict] = None) -> str:
    """Construct the full system prompt string.

    Args:
        context: Optional dict with keys:
            "company_name"  → str
            "company_id"    → int
            "events"        → List[dict] each with id, title, event_type,
                              end_time (ISO-8601), user_status
    """
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%A, %d %B %Y %H:%M UTC")

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
                    "event_type": "IRIS_DEADLINE|FORM_SUBMISSION|PPT|OA_WINDOW|INTERVIEW|CUSTOM|null",
                    "title": "string or null",
                    "action_link": "URL or null",
                    "start_time": "ISO-8601 UTC or null",
                    "end_time": "ISO-8601 UTC or null",
                    "status_note": "string or null",
                }
            ],
        },
        indent=2,
    )

    return f"""You are an assistant that outputs valid JSON conforming to the requested schema.
You are a placement-drive tracking assistant for NITK Surathkal.
Your sole task is to extract structured placement actions from Telegram channel messages.
Today is {now_str}.
{schedule_block}
## Operational Rules (apply in order)

1. AUDIENCE_SCOPE
   • If a single named student is addressed ("Nikhil Jain your interview is shifted to…"),
     set audience_scope="INDIVIDUAL".
   • All cohort-wide announcements use audience_scope="COHORT".

2. REPLACE_FORM
   • If a coordinator posts a new form URL with phrasing like "fill again", "new form",
     or it clearly supersedes an existing form link, set action="REPLACE_FORM".
   • action_link must contain the new URL.

3. EXTEND_DEADLINE
   • If a deadline is "extended", "shifted to", "pushed to", or a new date/time is given
     for an existing event, set action="EXTEND_DEADLINE".
   • Resolve relative references ("same timings", "Tuesday") using the Known Schedule State.

4. RESCHEDULE_EVENT
   • Use for full slot changes (new start_time + end_time) that are not simple extensions.

5. STATUS_NOTE
   • If the update is non-temporal ("results delayed", "shortlists pending", "no update yet"),
     set action="STATUS_NOTE" with a concise human-readable status_note.

6. NO_ACTION
   • If the message contains no placement-relevant action, set action="NO_ACTION".

7. DATETIME FORMAT
   • All times must be emitted as ISO-8601 UTC strings ending in 'Z'
     (e.g. "2026-10-15T23:59:00Z"). Never emit local times.

## Output Format (STRICT JSON — no markdown, no explanation)
{schema_example}

Return ONLY the JSON object above. No extra keys. No surrounding text. No code fences.
"""
