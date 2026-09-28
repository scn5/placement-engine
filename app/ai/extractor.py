"""
app/ai/extractor.py
LLM inference layer: builds prompt, calls the OpenAI-compatible API,
validates JSON output against the Pydantic schema, and returns a safe fallback
on any failure — never raising an unhandled exception to the caller.
"""
import logging
from typing import Optional

from pydantic import ValidationError

from app.ai.client import get_ai_client
from app.ai.prompts import build_system_prompt
from app.ai.schemas import LLMExtractionResult
from app.config import settings

logger = logging.getLogger(__name__)

# How many characters of raw LLM output to log on parse failures
_LOG_TRUNCATE = 500


async def extract_placement_actions(
    raw_text: str,
    current_context: Optional[dict] = None,
) -> LLMExtractionResult:
    """Call the LLM and return a validated LLMExtractionResult.

    Args:
        raw_text:        The consolidated Telegram message block to analyse.
        current_context: Optional schedule context dict injected into the prompt
                         (keys: company_name, company_id, events).

    Returns:
        A validated LLMExtractionResult.  On any API or parse error the function
        returns an empty LLMExtractionResult() instead of raising.
    """
    system_prompt = build_system_prompt(current_context)
    client = get_ai_client()

    try:
        response = await client.chat.completions.create(
            model=settings.LLM_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": raw_text},
            ],
            temperature=0.05,   # near-zero for deterministic structured output
            max_tokens=1024,
        )
    except Exception as exc:
        logger.error(
            "LLM API call failed for model=%s: %s",
            settings.LLM_MODEL,
            exc,
            exc_info=True,
        )
        return LLMExtractionResult()

    raw_content: str = (response.choices[0].message.content or "").strip()

    if not raw_content:
        logger.warning("LLM returned an empty response.")
        return LLMExtractionResult()

    try:
        result = LLMExtractionResult.model_validate_json(raw_content)
        logger.debug(
            "Extraction OK: company=%s actions=%d",
            result.company_name,
            len(result.actions),
        )
        return result
    except ValidationError as exc:
        logger.error(
            "LLM output failed schema validation.\nContent: %s\nErrors: %s",
            raw_content[:_LOG_TRUNCATE],
            exc,
        )
        return LLMExtractionResult()
    except Exception as exc:
        logger.error(
            "Unexpected JSON parse error.\nContent: %s\nError: %s",
            raw_content[:_LOG_TRUNCATE],
            exc,
        )
        return LLMExtractionResult()
