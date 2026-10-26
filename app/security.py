"""
app/security.py
Shared-key authentication for the API (Phase 0.6, gap C7).

Every router requires an X-API-Key header equal to settings.API_KEY, compared in
constant time; only /health is public. Production refuses to start without a key
of at least MIN_API_KEY_LENGTH characters (and, should it run anyway, rejects every
request). In development a missing key leaves the API open, with a warning at
startup.
"""
import logging
import secrets
from typing import Optional

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.config import settings

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"
MIN_API_KEY_LENGTH = 32
_GENERATE_HINT = 'python -c "import secrets; print(secrets.token_urlsafe(32))"'

_api_key_header = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,
    description="The shared API key set in the server's API_KEY and paired in the app.",
)


async def require_api_key(provided: Optional[str] = Security(_api_key_header)) -> None:
    """FastAPI dependency: the request must carry the configured API key."""
    expected = settings.api_key
    if expected is None:
        if settings.is_production:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The server has no API key configured.",
            )
        return  # development without a key: the API is open (warned at startup)
    if provided is None or not secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key.",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )


def check_api_key_configuration() -> None:
    """Run at API startup: production refuses to start without a strong key."""
    key = settings.api_key
    if settings.is_production:
        if key is None:
            raise RuntimeError(
                "API_KEY is not set, and the API refuses to start in production without it. "
                f"Generate one with: {_GENERATE_HINT}"
            )
        if len(key) < MIN_API_KEY_LENGTH:
            raise RuntimeError(
                f"API_KEY is shorter than {MIN_API_KEY_LENGTH} characters; the API refuses to start "
                f"in production with a weak key. Generate one with: {_GENERATE_HINT}"
            )
    elif key is None:
        logger.warning(
            "API_KEY is not set: every API route is open. Set API_KEY in .env (required in production)."
        )
