# PRODUCT REQUIREMENTS DOCUMENT (PRD)
**Product Name:** Placement Command Center
**Document Purpose:** Define product vision, user workflows, and business logic for AI coding assistants and human developers.

## 1. Product Vision & Problem Statement

### The Problem
Campus placement seasons are chaotic. Information is scattered across high-volume Telegram channels. Updates include shifting deadlines, tagged message chains, parallel interview slots, mutually exclusive role constraints, and unparsed Excel shortlists. 
Students currently suffer from:
1. **Information Overload:** Wading through hundreds of administrative chat messages to find one actionable deadline.
2. **Anxiety & Missed Deadlines:** Fear of missing a 2-hour "flash" deadline hidden in a threaded reply.
3. **Context Loss:** Losing track of whether a specific task (e.g., a Google Form) was already completed when a coordinator extends the deadline.

### The Solution
A strictly personal, Human-In-The-Loop (HITL) automated command center. 
1. **The Backend (The Scribe):** Listens to Telegram 24/7, uses LLMs to parse unstructured chaos into deterministic database events, deduplicates updates, and resolves threaded contexts.
2. **The Frontend (The Command Center):** A mobile app providing a clean, color-coded timeline of pending tasks, smart alarms, a triage inbox for new drives, and a directory of active tracks, entirely isolating the user from raw Telegram noise.

---

## 2. Target Audience & Tenancy
* **Audience:** A single 4th-year engineering student (The User) actively participating in campus placements.
* **Tenancy:** STRICTLY SINGLE-TENANT. This app runs on the user's personal infrastructure. There are no "friends", "cohorts", or multi-user login scopes. The system assumes exactly one human actor.
* **Scope Exclusions:** 
  * The UI strictly tracks **Full-Time Employment (FTE)** and **COMBINED** drives. 
  * **INTERNSHIP** drives are silently logged to the database for historical record but are fully hidden from all UI components (Timeline, Triage, Directory) to prevent visual clutter.

---

## 3. Core User Journeys & Workflows

### Workflow 1: New Drive Discovery & Triage
* **Context:** The main placement channel posts an invite link to a new company (e.g., "ZS Associates Drive").
* **Action:** The backend auto-joins the subchannel, parses the roles (DAA, BTSA), defaults their tracking status to `SILENT_TRACK`, and sends a one-time push notification: "New Drive Discovered: ZS Associates".
* **Expected Result:** The user opens the **Triage Hub** in the app, sees the CTC and role details, and decides to track the DAA role (moves to `ELIGIBLE`) while ignoring the BTSA role (moves to `IGNORED`).

### Workflow 2: The Morning Timeline Review
* **Context:** The backend ingested 30 messages overnight.
* **Action:** The user opens the Timeline tab.
* **Expected Result:** A chronological list of actionable events (e.g., "ZS Associates - Aptitude Test"). Events requiring action are badged as **Blue (PENDING)**.
* **Attachment Indicator:** If an event has a paperclip icon, the user knows an unparsed PDF/Excel file was attached to the original message, prompting them to manually check the raw Telegram channel.

### Workflow 3: Task Execution & The Hitlist
* **Context:** The user sees a Blue (PENDING) "Google Form Submission" due at 4 PM.
* **Action:** The user clicks the `action_link`, fills the form, and taps the checkbox in the app.
* **Expected Result:** The task instantly turns **Green (COMPLETED_ACTIVE)**. Background notification alarms for this task are cancelled. When the deadline passes, it turns **Grey (COMPLETED_EXPIRED)**.

### Workflow 4: The Deadline Extension (Silent Update)
* **Context:** The user already marked a task as Green (COMPLETED). The coordinator posts: *"Deadline extended to 8 PM."*
* **Expected Result:** The backend updates the database `start_time`/`end_time` to 8 PM. The task **remains Green (COMPLETED)**, respecting that the user already did the work and preventing false-alarm anxiety.

### Workflow 5: The Form Replacement / Invalid Link
* **Context:** The coordinator posts: *"The previous link was broken. Everyone must refill this new form by 9 PM."*
* **Expected Result:** The backend archives the old completion state, updates the link, and **forcefully resets the user state to Blue (PENDING)**. New alarms are instantly armed.

### Workflow 6: The "Defaulter List" Safety Net
* **Context:** A message drops: *"The following 50 students entered the wrong email: [List of Names/Roll Numbers]"*.
* **Expected Result:** The LLM cross-references the message text against the user's personal details (`system_configs`). Because the user's roll number is NOT in the list, the system drops the message as irrelevant. The user's phone never buzzes.

### Workflow 7: Criteria Relaxation & Re-Triage
* **Context:** The user previously put a Software Engineer role into `SILENT_TRACK` because the CGPA cutoff was 8.0 and they did not qualify. The role has been silently logging updates. The coordinator posts: *"The company has reduced the CGPA cutoff to 7.0."*
* **Action:** The LLM detects the criteria shift. Because the role is in `SILENT_TRACK`, the backend sets `is_triaged` back to `false` and fires a high-priority push notification: *"Criteria Update: [Company Name]"*. (If the role was already `ELIGIBLE`, the system would silently log the note without alerting the user).
* **Expected Result:** The role reappears in the user's **Triage Hub** with an amber `⚡ CRITERIA UPDATE` badge and the note about the CGPA change. If the user now qualifies, they tap "Eligible" to activate the timeline. If they still don't qualify, they tap "Silent Track" to send it back to the waiting room.
---

## 4. Functional Requirements (FR)

### FR1: Environment Isolation (Main vs. Test)
* **Requirement:** The app must support a seamless toggle between "Production" and "Sandbox" data.
* **Implementation:** The mobile app features a persistent top-level toggle. Toggling it filters all backend API requests (`?env=MAIN` or `?env=TEST`). Main feed data and testing data must never cross-pollinate.

### FR2: Smart Notification Engine
* **Requirement:** The user must be reliably alerted before deadlines, but not spammed for completed tasks.
* **Implementation:** A polling worker checks the DB every 60 seconds. If an event is `PENDING` and exactly **2 hours** or **30 minutes** away from `start_time`, push a high-priority FCM notification. If it is `COMPLETED`, remain silent.

### FR3: Profile Configuration & Zero-Config Pairing
* **Requirement:** The app must pair easily with the backend and provide the LLM with user context.
* **Implementation:** 
  * The app performs a `GET /health` preflight check to save the backend URL to local storage.
  * A Settings screen allows the user to input: `student_name`, `roll_number`, `personal_email`, `college_email`, `main_feed_id`, and `test_feed_id`. 

### FR4: Role-Level Mutually Exclusive Warnings
* **Requirement:** Users must be warned if applying to multiple roles within the same company will lead to disqualification.
* **Implementation:** The LLM detects exclusive constraints and populates `lifecycle_note`. The UI displays a warning banner in the Triage Hub and Company Directory (e.g., "WARNING: Mutually exclusive. Apply for only one role.").

### FR5: Parallel Interview Slot Resolution
* **Requirement:** When multiple interview slots are announced (e.g., Panel A, Panel B), the user shouldn't see deadlines that don't apply to them.
* **Implementation:** 
  1. If the message maps specific roll numbers to panels, the LLM extracts *only* the user's designated panel.
  2. If the message is generic, the LLM extracts all panels as separate timeline events, allowing the user to manually "Ignore/Delete" irrelevant ones.

### FR6: Manual Injections (Custom Overrides)
* **Requirement:** The user needs to track off-campus referrals or offline events.
* **Implementation:** A floating `[+]` button provides form inputs for "Add Custom Company" and "Add Custom Stage", bypassing Telegram ingestion entirely.

---

## 5. Out of Scope (Anti-Goals)
*To prevent scope creep and AI over-engineering, the following are strictly prohibited in Version 1:*
1. **Multi-User Auth:** No JWT login screens, no Firebase Auth, no OAuth. The API assumes the device holding the app is the sole owner.
2. **Automated File Parsing:** No PDF, Excel, or Image OCR extraction. If a shortlist drops as a document, the system extracts the event based on the caption text and flags it with `has_attachments = true`. The user must manually read the document.
3. **Automated Submissions:** The system tracks forms and provides links, but it will **never** attempt to script/automate the actual filling of Google Forms or Superset profiles.
4. **Web Dashboard:** The frontend is exclusively an Expo React Native mobile app. No React/Next.js web frontend is required.