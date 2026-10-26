"""
app/ai/client.py
AsyncOpenAI client factory with configurable base_url for OpenAI-compatible gateways
(OpenRouter, custom proxy, local vLLM, etc.).
"""
from typing import Optional
from openai import AsyncOpenAI

from app.config import settings

_ai_client: Optional[AsyncOpenAI] = None


def get_ai_client() -> AsyncOpenAI:
    """Return a module-level cached singleton AsyncOpenAI client bound to the configured endpoint.

    Settings used:
        LLM_API_KEY   — required; bearer token for the endpoint.
        LLM_BASE_URL  — optional; override to point at OpenRouter / local server.
                        If None the official OpenAI endpoint is used.
    """
    global _ai_client
    if _ai_client is None:
        api_key = settings.LLM_API_KEY or settings.GROQ_API_KEY or "no-key"
        # Transient errors (429, 5xx, timeouts) are retried here first; whatever
        # still fails reaches the message ledger's retry backoff.
        kwargs = {"api_key": api_key, "max_retries": 3}
        if settings.LLM_BASE_URL:
            kwargs["base_url"] = settings.LLM_BASE_URL
        _ai_client = AsyncOpenAI(**kwargs)
    return _ai_client
