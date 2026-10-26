"""
app/ai/extractor.py
LLM inference layer: builds the prompt, calls the OpenAI-compatible API and
validates the JSON output against the Pydantic schema.

A valid result — including one with no actions — is returned. Anything else
(request errors after the client's own retries, timeouts, a truncated, empty or
invalid answer) raises ExtractionFailed: the batch is then recorded FAILED in the
message ledger and retried with backoff (PROJECT_STATE §7.6). There is no
regex fallback — failed extractions are retried, never guessed (gap C5).

Output modes, tried in order:
  1. Groq structured outputs in strict mode (json_schema): decoding is
     constrained to RESULT_JSON_SCHEMA. If Groq rejects the request but JSON-object
     mode then works, strict mode is switched off for the rest of the process.
  2. JSON-object mode (response_format json_object).
  3. On Groq's json_validate_failed: no response format, JSON cut out of the text.
"""
import logging
import re
from typing import Optional

from openai import BadRequestError, OpenAIError
from pydantic import ValidationError

from app.ai.client import get_ai_client
from app.ai.prompts import build_system_prompt
from app.ai.schemas import RESULT_JSON_SCHEMA, LLMExtractionResult
from app.config import settings

logger = logging.getLogger(__name__)

# How many characters of raw LLM output to log on parse failures
_LOG_TRUNCATE = 500
# Room for gpt-oss reasoning tokens as well as the JSON answer.
_MAX_COMPLETION_TOKENS = 8192
_REQUEST_TIMEOUT_SECONDS = 60.0

_STRICT_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "placement_actions", "strict": True, "schema": RESULT_JSON_SCHEMA},
}
_JSON_OBJECT_FORMAT = {"type": "json_object"}

# Switched off for the rest of the process if Groq rejects strict mode.
_strict_mode = True


class ExtractionFailed(Exception):
    """The LLM gave no usable answer; the batch is recorded FAILED and retried."""


def _clean_json_string(raw: str) -> str:
    """Clean and extract raw JSON object string from LLM response.
    - Strips leading/trailing whitespace.
    - Strips markdown code blocks (e.g. removes ```json and ```).
    - If the text still contains wrapper text, extracts the substring from the first '{' to the last '}'.
    """
    if not raw:
        return ""
    text = raw.strip()

    # Strip markdown code blocks if wrapped
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    # Extract substring from the first '{' to the last '}'
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace >= first_brace:
        text = text[first_brace : last_brace + 1].strip()

    return text


def _error_body(exc: BadRequestError) -> str:
    return f"{exc} {getattr(exc, 'body', '') or ''}"


async def _complete(client, messages: list, response_format: Optional[dict]) -> str:
    """One completion; returns its text or raises ExtractionFailed. A
    BadRequestError is re-raised for the caller to choose a fallback."""
    kwargs = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": 0.0,
        "max_completion_tokens": _MAX_COMPLETION_TOKENS,
        "timeout": _REQUEST_TIMEOUT_SECONDS,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    if settings.LLM_REASONING_EFFORT:
        kwargs["reasoning_effort"] = settings.LLM_REASONING_EFFORT
    try:
        response = await client.chat.completions.create(**kwargs)
    except BadRequestError:
        raise
    except OpenAIError as exc:
        raise ExtractionFailed(f"LLM request failed ({type(exc).__name__})") from exc

    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise ExtractionFailed("LLM output was cut off at the token limit")
    content = (choice.message.content or "").strip()
    if not content:
        raise ExtractionFailed("LLM returned an empty response")
    return content


async def _completion_text(messages: list) -> str:
    global _strict_mode
    client = get_ai_client()

    if _strict_mode:
        try:
            return await _complete(client, messages, _STRICT_FORMAT)
        except BadRequestError as strict_exc:
            logger.warning("Strict JSON-schema request rejected: %s", str(strict_exc).splitlines()[0])
            try:
                content = await _complete(client, messages, _JSON_OBJECT_FORMAT)
            except BadRequestError as exc:
                raise ExtractionFailed(f"LLM request rejected ({type(exc).__name__})") from exc
            _strict_mode = False
            logger.warning("Strict JSON-schema mode turned off for this process; using JSON-object mode.")
            return content

    try:
        return await _complete(client, messages, _JSON_OBJECT_FORMAT)
    except BadRequestError as exc:
        if "json_validate_failed" not in _error_body(exc):
            raise ExtractionFailed(f"LLM request rejected ({type(exc).__name__})") from exc
    logger.warning("[AI] JSON-object mode failed validation. Retrying without a response format...")
    try:
        return await _complete(client, messages, None)
    except BadRequestError as exc:
        raise ExtractionFailed(f"LLM request rejected ({type(exc).__name__})") from exc


async def extract_placement_actions(
    raw_text: str,
    current_context: Optional[dict] = None,
) -> LLMExtractionResult:
    """Call the LLM and return a validated LLMExtractionResult.

    Args:
        raw_text:        The message block to analyse (see prompts.format_message_block).
        current_context: Optional schedule context injected into the prompt
                         (see prompts.build_system_prompt).

    Raises:
        ExtractionFailed: no usable answer — the caller records the batch FAILED.
    """
    system_prompt = build_system_prompt(current_context)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": raw_text},
    ]
    raw_content = await _completion_text(messages)

    try:
        result = LLMExtractionResult.model_validate_json(_clean_json_string(raw_content))
    except ValidationError as exc:
        logger.warning(
            "LLM output does not match the schema (%d error(s)). Content: %s",
            exc.error_count(),
            raw_content[:_LOG_TRUNCATE],
        )
        raise ExtractionFailed(f"LLM output does not match the schema ({exc.error_count()} error(s))") from exc

    logger.debug(
        "Extraction OK: company=%s actions=%d",
        result.company_name,
        len(result.actions),
    )
    return result
