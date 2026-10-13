"""
app/ai/client.py
AsyncOpenAI client factory with configurable base_url for OpenAI-compatible gateways
(OpenRouter, custom proxy, local vLLM, etc.).
"""
from openai import AsyncOpenAI

from app.config import settings


def get_ai_client() -> AsyncOpenAI:
    """Return a fresh AsyncOpenAI client bound to the configured endpoint.

    Settings used:
        LLM_API_KEY   — required; bearer token for the endpoint.
        LLM_BASE_URL  — optional; override to point at OpenRouter / local server.
                        If None the official OpenAI endpoint is used.
    """
    api_key = settings.LLM_API_KEY or settings.GROQ_API_KEY or "no-key"
    kwargs = {"api_key": api_key}
    if settings.LLM_BASE_URL:
        kwargs["base_url"] = settings.LLM_BASE_URL
    return AsyncOpenAI(**kwargs)
