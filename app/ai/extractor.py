"""
app/ai/extractor.py
LLM inference layer: builds prompt, calls the OpenAI-compatible API,
validates JSON output against the Pydantic schema, and returns a safe fallback
on any failure — never raising an unhandled exception to the caller.
"""
import logging
import re
from typing import Optional

from openai import BadRequestError
from pydantic import ValidationError

from app.ai.client import get_ai_client
from app.ai.prompts import build_system_prompt
from app.ai.schemas import LLMExtractionResult
from app.config import settings

logger = logging.getLogger(__name__)

# How many characters of raw LLM output to log on parse failures
_LOG_TRUNCATE = 500


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
    if "You are an assistant that outputs valid JSON conforming to the requested schema." not in system_prompt:
        system_prompt = (
            "You are an assistant that outputs valid JSON conforming to the requested schema.\n"
            + system_prompt
        )
    client = get_ai_client()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": raw_text},
    ]

    response = None
    try:
        response = await client.chat.completions.create(
            model=settings.LLM_MODEL,
            response_format={"type": "json_object"},
            messages=messages,
            temperature=0.0,
            max_tokens=4096,
            timeout=30.0,
        )
    except BadRequestError as exc:
        err_msg = str(exc)
        if hasattr(exc, "body") and exc.body:
            err_msg += f" {exc.body}"

        if "json_validate_failed" in err_msg:
            logger.warning("[AI] Primary JSON mode failed. Retrying without response_format constraint...")
            try:
                fallback_response = await client.chat.completions.create(
                    model=settings.LLM_MODEL,
                    messages=messages,
                    temperature=0.0,
                    max_tokens=4096,
                    timeout=30.0,
                )
                fallback_raw: str = (fallback_response.choices[0].message.content or "").strip()
                if not fallback_raw:
                    logger.warning("LLM fallback returned an empty response.")
                    return LLMExtractionResult()

                cleaned = _clean_json_string(fallback_raw)
                result = LLMExtractionResult.model_validate_json(cleaned)
                logger.info(
                    "Fallback extraction OK: company=%s actions=%d",
                    result.company_name,
                    len(result.actions),
                )
                return result
            except ValidationError as v_exc:
                logger.error(
                    "LLM fallback output failed schema validation.\nContent: %s\nErrors: %s",
                    fallback_raw[:_LOG_TRUNCATE] if 'fallback_raw' in locals() else "",
                    v_exc,
                )
                return LLMExtractionResult()
            except Exception as f_exc:
                logger.error(
                    "LLM fallback API call or parsing failed for model=%s: %s",
                    settings.LLM_MODEL,
                    f_exc,
                    exc_info=True,
                )
                return LLMExtractionResult()
        else:
            logger.error(
                "LLM API call failed with BadRequestError for model=%s: %s",
                settings.LLM_MODEL,
                exc,
                exc_info=True,
            )
            return LLMExtractionResult()
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

    cleaned = _clean_json_string(raw_content)

    try:
        result = LLMExtractionResult.model_validate_json(cleaned)
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
