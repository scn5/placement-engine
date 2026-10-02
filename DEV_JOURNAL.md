# placement-engine/DEV_JOURNAL.md

[September 2026]: Telethon Message Fragmentation & LLM Context Loss
The Problem: Inbound Telegram messages in subchannels were often split across multiple fragmented posts, causing the Groq LLM to process incomplete context and fail entity extraction.
The Context/Why it failed: Telethon triggers `events.NewMessage` instantly for every individual chat bubble. Processing these sequentially led to redundant API calls and missing relational data.
The Decision/Fix: Implemented an in-memory sliding buffer with a 90 to 120-second debounce window. Messages are now held and merged into a single cohesive payload before triggering the Groq extraction logic.

[September 2026]: Thread Disconnection in Main Feed
The Problem: Replies containing schedule tables or updates posted in the main placement feed lost their association with the original company announcement.
The Context/Why it failed: Standard chronological parsing couldn't mathematically link detached follow-up schedules to their parent roles without explicit context.
The Decision/Fix: Leveraged Telegram's `reply_to_msg_id` header in the Telethon listener to stitch replies directly to the parent announcement. Rule established: Always map timeline events to the correct company using relational thread IDs.

[September 2026]: High Groq API Token Consumption & Noise
The Problem: The Groq LLM was frequently triggered by administrative noise, SPOC greetings, and non-temporal chatter, wasting tokens and risking hallucinations.
The Context/Why it failed: Routing every raw broadcast directly to the LLM without pre-filtering caused unnecessary API overhead and degraded the extraction quality.
The Decision/Fix: Built a deterministic regex pre-filter that automatically drops non-temporal messages and administrative noise before they reach the LLM pipeline. Rule established: The LLM is strictly reserved for high-signal payloads.

[September 2026]: Missed In-Place Schedule Modifications
The Problem: Deadlines and venues modified by placement coordinators hours after the initial announcement were not updating in the PostgreSQL database.
The Context/Why it failed: The Telethon listener was exclusively bound to `events.NewMessage`, completely ignoring subsequent message modifications in the Telegram channels.
The Decision/Fix: Registered the `events.MessageEdited` handler in Telethon. The system now tracks entities by unique `message_id` to catch and parse in-place modifications silently without duplicating database records.

[September 2026]: Missing Triage Endpoint (Frontend 404)
The Problem: The mobile application threw a `Not Found` error when attempting to load the Triage Hub staging queue.
The Context/Why it failed: The frontend API contract expected a `/triage` endpoint to fetch the manual-review queue, but `main.py` lacked this router in FastAPI.
The Decision/Fix: A temporary fallback was added to the frontend to catch the 404. Rule established: The backend must formally implement `@app.get("/triage")` and `@app.post("/triage/{id}/accept")` in FastAPI to resolve the API mismatch.