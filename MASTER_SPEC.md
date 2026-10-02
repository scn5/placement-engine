# MASTER SPECIFICATION: PLACEMENT COMMAND CENTER (AI-OPTIMIZED)

## 1. System Architecture & Core Principles

* **Core Philosophy:** **Human-in-the-Loop (HITL)**. The system acts as a scribe/assistant to extract structured data and manage schedules. The user maintains ultimate authority over task completion states.
* **Tenant Model:** **Single-Tenant by Design**. Database, state machine, and notification hooks are optimized for personal operation. No multi-tenant isolation or `users` table exists.
* **Backend Runtime:** Python **FastAPI** hosted permanently on an **Oracle Cloud Infrastructure (OCI)** Always Free Ubuntu VM inside isolated Docker containers with local volume persistence.
* **Database:** **PostgreSQL** (Dockerized). Serves as the single source of truth.
* **Extraction LLM Engine:** Parsing relies on **`openai/gpt-oss-20b` hosted via Groq LPU** inference using an OpenAI-compatible endpoint. Enforces structured output via JSON schema serialization.
* **Networking:** **Cloudflare Tunnel** for outbound HTTPS connection and SSL management.
* **Notifications:** **Firebase Cloud Messaging (FCM) / Expo Push**. Used strictly as a notification transport mechanism to wake the React Native mobile app.

---

## 2. Data Ingestion & Telegram Pipeline

* **Auto-Join & Triage Trigger:** When the Telethon worker detects a valid Telegram invite link (e.g., `t.me/+...`) in the Main Feed and the test feed, it automatically executes a join request for that subchannel. Upon successful join, the backend initializes the company/roles with a default status of `SILENT_TRACK` and fires a one-time "New Drive Triage" push notification to the user's app.
* **Ingress Decoupling:** The Telethon listener acts strictly as an asynchronous producer, writing inbound MTProto events into an internal `asyncio.Queue`. Downstream tasks (debouncing, LLM inference, DB writes) execute in decoupled background consumers to prevent dropped MTProto keep-alives.
* **Timezone Normalization:** All Telegram message timestamps (UTC) are converted to `Asia/Kolkata` (UTC+05:30) before being supplied as reference times to the LLM.
* **Subchannel Debounce Buffer:** Inbound messages are held in an in-memory sliding buffer for **90 to 120 seconds** of inactivity to merge fragment bursts into a cohesive block.
* **Thread Resolution (Recursive Fetching):** To handle tagged messages, the system recursively fetches `reply_to_msg_id` up to a **depth of 6**. The entire chronological chain is concatenated for LLM context.
* **Main Feed Thread Stitching:** Companies track a `main_feed_msg_ids` array. New messages replying to any ID in this array map to that company, and the new ID is appended to the array.
* **24-Hour Bounded Backfill:** When a new subchannel link is discovered, the system crawls messages strictly within the **last 24 hours** to bootstrap schedules without pulling obsolete clutter.
* **Environment Tagging:** The system checks incoming `chat_id`s against `system_configs`. Newly tracked companies are stamped with `environment_type = 'MAIN'` or `'TEST'`, ensuring data isolation in the UI.
* **Rate-Limit Hardening:** Active RPC queries enforce exponential backoff (max 300s) for `FloodWaitError`. A 60-second background heartbeat job pings the Telegram DC to detect/reset half-open TCP states.
* **Unparsed Attachment Flags:** The Telethon worker flags messages containing unparsed documents (PDF, Excel) with a has_attachments boolean payload. The UI uses this flag to render a paperclip icon on the timeline event, alerting the user to manually check the original Telegram message for external context.
---

## 3. LLM Extraction Engine & Rules (The Brain)

When payloads pass pre-filters (dropping SPOC greetings/chatter), the backend fetches a snapshot of the **Current Database State** (active events) and **Personal Profile Config**, injecting them into the Groq LLM prompt.

* **Profile Defaulter Filter:** The LLM checks names/roll numbers against the user's `system_configs`. If the message lists defaulters and the user's details are **not** present, the LLM outputs `is_actionable = false`, dropping the payload.
* **Shortlist Tracking:** If a shortlist drops as text, the LLM checks for the user's details. If found, it updates the role track's `lifecycle_status` (e.g., `INTERVIEWS`); it **does not** create a timeline event.
* **Strict Event Categories:** `event_category` must be one of: `['RESUME', 'IRIS', 'FORM', 'APPLICATION', 'PPT', 'OA', 'GD', 'INTERVIEW', 'CUSTOM']`.
* **State-Aware Round Counting (OA/Interview Logic):**
  * If a message announces an event without specifying a round, the LLM checks the database snapshot.
  * If the previous event's (e.g., OA 1) `end_time` has **passed**, extract as `round_number = N + 1`.
  * If the previous event's `end_time` is in the **future**, extract as an update to the current round.
  * *Boundary Failsafe:* If only a `start_time` is provided for an OA, backend defaults `end_time = start_time + 2 hours`.
* **Parallel Slot Extraction:** If multiple interview/GD slots are announced, the LLM extracts the slot matching the user's roll number. If no roll numbers are present, it extracts all slots as separate timeline events for manual UI deletion.
* **Form Replacement Override:** If text implies a form is invalid or must be refilled, LLM sets `is_form_replacement = true`. The backend increments `version`, updates deadlines, and forcefully resets `user_status` to `PENDING`, re-arming alarms.
* **Mutually Exclusive Role Warnings:** If a message dictates that students can apply for only one role among multiple options, the LLM must populate this constraint into the `lifecycle_note` of the extracted roles (e.g., "WARNING: Mutually exclusive. Apply for only one role.").
---

## 4. JSON Payload Contract (AI Schema)

The backend enforces this exact Pydantic/JSON Schema constraint on the Groq API call:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "PlacementExtractionPayload",
  "type": "object",
  "required": ["is_actionable", "audience_scope", "roles", "events"],
  "properties": {
    "is_actionable": { "type": "boolean" },
    "audience_scope": { "type": "string", "enum": ["COHORT", "INDIVIDUAL"] },
    "company_name": { "type": ["string", "null"] },
    "roles": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["role_name", "drive_type"],
        "properties": {
          "role_name": { "type": "string" },
          "drive_type": { "type": "string", "enum": ["FTE", "INTERNSHIP", "COMBINED"] },
          "ctc_stipend_info": { "type": ["string", "null"] },
          "lifecycle_note": { "type": ["string", "null"] }
        }
      }
    },
    "events": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["event_category", "round_number", "title", "start_time", "is_form_replacement"],
        "properties": {
          "event_category": {
            "type": "string",
            "enum": ["RESUME", "IRIS", "FORM", "APPLICATION", "PPT", "OA", "GD", "INTERVIEW", "CUSTOM"]
          },
          "round_number": { "type": "integer" },
          "title": { "type": "string" },
          "venue": { "type": ["string", "null"] },
          "action_link": { "type": ["string", "null"] },
          "start_time": { "type": "string", "format": "date-time", "description": "Primary deadline or start time of event." },
          "end_time": { "type": ["string", "null"], "format": "date-time", "description": "Null for instantaneous deadlines. For time slots, backend defaults to start_time + 2 hours if missing." },
          "is_form_replacement": { "type": "boolean" }
        }
      }
    }
  }
}
```

---

## 5. PostgreSQL Database Schema

```sql
-- 1. SYSTEM CONFIGS (Personal Profile & Environment)
CREATE TABLE system_configs (
    id SERIAL PRIMARY KEY,
    student_name VARCHAR(100),
    roll_number VARCHAR(50),
    personal_email VARCHAR(100),
    college_email VARCHAR(100),
    main_feed_id BIGINT,          -- Production Telegram Channel ID
    test_feed_id BIGINT,          -- Sandbox/Test Channel ID
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 2. COMPANIES
CREATE TABLE companies (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    normalized_name VARCHAR(255) UNIQUE NOT NULL,
    environment_type VARCHAR(10) DEFAULT 'MAIN',  -- 'MAIN' or 'TEST'
    main_feed_msg_ids BIGINT[] DEFAULT '{}',      
    subchannel_id BIGINT UNIQUE,
    is_active BOOLEAN DEFAULT TRUE,
    is_custom BOOLEAN DEFAULT FALSE,              
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 3. ROLE TRACKS
CREATE TABLE role_tracks (
    id SERIAL PRIMARY KEY,
    company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    role_name VARCHAR(255) NOT NULL,              
    drive_type VARCHAR(32) DEFAULT 'FTE',         
    ctc_stipend_info TEXT,
    tracking_mode VARCHAR(32) DEFAULT 'SILENT_TRACK', -- Defaults to SILENT for UI Triage
    lifecycle_status VARCHAR(64) DEFAULT 'APPLICATION_WINDOW',
    latest_status_note TEXT,                      
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 4. TIMELINE EVENTS
CREATE TABLE timeline_events (
    id SERIAL PRIMARY KEY,
    company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    role_track_id INTEGER REFERENCES role_tracks(id) ON DELETE SET NULL,
    event_category VARCHAR(32) NOT NULL,          -- Strict ENUM matching LLM rules
    round_number INTEGER DEFAULT 1,               
    title VARCHAR(255) NOT NULL,                  
    action_link TEXT,
    start_time TIMESTAMP WITH TIME ZONE NOT NULL,
    end_time TIMESTAMP WITH TIME ZONE,
    venue VARCHAR(255),
    user_status VARCHAR(32) DEFAULT 'PENDING',    -- 'PENDING' or 'COMPLETED'
    version INTEGER DEFAULT 1,                    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

### 5.1 Deduplication & Data Retention
* **Deduplication Key:** `(company_id, role_track_id, event_category, round_number)`. Updates matching this key modify `end_time` and `action_link` in place.
* **Status Independence:** The ingestion update pipeline **strictly ignores `user_status`**. If a deadline is extended, a `COMPLETED` task remains `COMPLETED` (unless `is_form_replacement = true`).
* **Retention & Purge:** Indefinite retention. Deletions are exclusively user-driven. Deleting a `Company` cascades via `ON DELETE CASCADE`.

---

## 6. Event State Machine & Notification Rules

### 6.1 State Machine Lifecycle
```text
                     [ Event Created / Extracted ]
                                   │
                                   ▼
                            ┌───────────┐
                ┌───────────│  PENDING  │───────────┐
                │           └───────────┘           │
       (User checks done)         │          (Deadline passes)
                │                 │                 │
                ▼                 │                 ▼
         ┌──────────────┐         │           ┌───────────┐
         │ COMPLETED_   │         │           │  MISSED   │ ◄─── (Solid Red Badge)
         │    ACTIVE    │         │           └───────────┘
         │  (Vibrant    │         │                 │
         │    Green)    │         │        (Deadline extended later)
         └──────────────┘         │                 │
                │                 │                 ▼
        (Deadline passes)         │           ┌───────────┐
                │                 └───────────│  PENDING  │ ── (Alarms Re-armed)
                ▼                             └───────────┘
         ┌──────────────┐
         │ COMPLETED_   │
         │   EXPIRED    │
         │ (Slate Grey) │
         └──────────────┘
```

### 6.2 UI Color Matrix
Mapped dynamically on the client based on `user_status` and `NOW() > end_time`:

| Task Status (`user_status`) | Deadline Status (`end_time`) | UI Color | State Meaning |
| :--- | :--- | :--- | :--- |
| `PENDING` | Active (Future) | **Blue** | Task needs to be done. |
| `PENDING` | Expired (Past) | **Red** | Deadline missed/unfilled. |
| `COMPLETED` | Active (Future) | **Green** | Done, waiting for deadline to close. |
| `COMPLETED` | Expired (Past) | **Grey** | Done, and form is now closed. |

### 6.3 Polling Notification Scheduler
A stateless FastAPI background task queries PostgreSQL every **60 seconds**.
* **Query Condition:** `user_status = 'PENDING'` AND `start_time` is exactly **2 hours** or **30 minutes** away.
* **Behavior:** If met, an FCM push payload is dispatched. Completing a task inherently disables future alarms, as it drops out of the `PENDING` query.

---

## 7. Mobile Client Interface (`command-center`)

* **FR-7.1 Server Connectivity & Zero-Config Pairing:**
  * User inputs the backend base URL (Cloudflare HTTPS tunnel or local LAN IP).
  * System executes a preflight `GET /health` verification before granting entry.
  * Preserves verified endpoint in persistent local storage (`AsyncStorage`).
* **FR-7.2 Environment Toggle (View Isolation):**
  * Persistent top-level switch (Main / Test). When toggled, the app executes requests with `?env=MAIN` or `?env=TEST`. The backend filters by `companies.environment_type`. Main and Test data never cross-pollinate.
* **FR-7.3 Timeline Screen:**
  * Displays chronologically ordered stage deadlines grouped by date.
  * Visual status badges (Blue, Red, Green, Grey) rendering task state.
  * *Global Masking:* Internship role tracks are strictly hidden from this view to prevent clutter.
  * Includes paperclip indicators on events flagged with `has_attachments`.
  * Pull-to-refresh sync with `GET /timeline`.
* **FR-7.4 Triage Hub:**
  * Dedicated view for reviewing incoming placement notices (which default to `SILENT_TRACK`).
  * Action triggers applied per role: **Accept** (moves to `ELIGIBLE` & active timeline), **Edit** (amends metadata), or **Discard** (moves to `IGNORED`).
* **FR-7.5 Companies Directory:**
  * Searchable list by company name or role.
  * Filter categories: `All`, `Eligible`, `Silent Track`, `Ignored`.
  * Detail views showing full placement drive history, package details, mutually exclusive warnings, and stage progression pipelines.
* **FR-7.6 Manual Drive / Stage Creation:**
  * Global floating action trigger providing options: "Add Custom Company" and "Add Custom Stage".
  * Form inputs for manual overrides outside automated Telegram broadcasts.
* **FR-7.7 Profile Settings Screen:**
  * Secure UI to configure `student_name`, `roll_number`, emails, and Telegram Feed IDs (writes to `system_configs`). 