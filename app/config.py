from typing import Optional

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENVIRONMENTS = {"MAIN", "TEST"}


class Settings(BaseSettings):
    # Database — production must set DATABASE_URL explicitly.
    DATABASE_URL: str = "sqlite+aiosqlite:///./dev.db"
    # development | testing | production. Any other value counts as production
    # (fail-closed): API docs are off and API_KEY is required.
    APP_ENV: str = "development"
    # Shared key the app sends as X-API-Key (Phase 0.6). Required in production
    # (at least 32 characters); without one in development every route is open.
    API_KEY: Optional[SecretStr] = None

    # Telegram MTProto credentials (TELEGRAM_API_ID=0 disables the ingestion worker)
    TELEGRAM_API_ID: int = 0
    TELEGRAM_API_HASH: str = ""
    TELEGRAM_SESSION_NAME: str = "placement_session"
    TELEGRAM_SESSION_STRING: str = ""
    TELEGRAM_PHONE: Optional[str] = None

    # Channel routing
    MAIN_FEED_CHANNEL_ID: Optional[int] = None
    TEST_FEED_CHANNEL_ID: Optional[int] = None
    # Feeds the worker reads, comma-separated. Defaults to TEST only, so a
    # development machine never processes the real feed by accident;
    # production sets "MAIN,TEST".
    INGEST_ENVIRONMENTS: str = "TEST"

    # Debounce window (seconds); reduce to 0.1 in test suites
    DEBOUNCE_DELAY_SECONDS: float = 90.0
    # A busy chat is flushed at most this long after its first buffered message.
    DEBOUNCE_MAX_WAIT_SECONDS: float = 120.0

    # Ingestion worker loops (seconds / hours)
    CONFIG_REFRESH_SECONDS: float = 60.0
    CATCHUP_INTERVAL_SECONDS: float = 900.0
    CATCHUP_MAX_AGE_HOURS: float = 72.0
    RETRY_POLL_SECONDS: float = 60.0
    HEARTBEAT_SECONDS: float = 30.0
    # Optional dead-man's-switch URL, pinged every 5 minutes while Telegram is connected.
    DEADMAN_URL: Optional[str] = None

    # LLM inference (OpenAI-compatible endpoint)
    LLM_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    LLM_BASE_URL: Optional[str] = "https://api.groq.com/openai/v1"
    LLM_MODEL: str = "openai/gpt-oss-20b"
    # low | medium | high; unset = the provider's default. Lower effort is cheaper
    # and faster but may resolve dates less carefully — tune with shadow-run data.
    LLM_REASONING_EFFORT: Optional[str] = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("INGEST_ENVIRONMENTS")
    @classmethod
    def _validate_environments(cls, value: str) -> str:
        envs = sorted({part.strip().upper() for part in value.split(",") if part.strip()})
        unknown = set(envs) - _ENVIRONMENTS
        if not envs or unknown:
            raise ValueError(
                f"INGEST_ENVIRONMENTS must list MAIN and/or TEST, got {value!r}"
            )
        return ",".join(envs)

    @field_validator("LLM_REASONING_EFFORT")
    @classmethod
    def _validate_reasoning_effort(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        value = value.strip().lower()
        if value not in ("low", "medium", "high"):
            raise ValueError(f"LLM_REASONING_EFFORT must be low, medium or high, got {value!r}")
        return value

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.strip().lower() not in ("development", "testing")

    @property
    def api_key(self) -> Optional[str]:
        """The configured API key, or None when unset or blank."""
        value = self.API_KEY.get_secret_value().strip() if self.API_KEY is not None else ""
        return value or None

    @property
    def ingest_environments(self) -> frozenset[str]:
        return frozenset(self.INGEST_ENVIRONMENTS.split(","))

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.TELEGRAM_API_ID) and bool(self.TELEGRAM_API_HASH)


settings = Settings()
