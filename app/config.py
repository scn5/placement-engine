from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql+asyncpg://antitele:StrongPasswordHere123!@localhost:5432/anti_tele_db"
    APP_ENV: str = "development"

    # Telegram MTProto credentials
    TELEGRAM_API_ID: int = 0
    TELEGRAM_API_HASH: str = ""
    TELEGRAM_SESSION_NAME: str = "placement_session"
    TELEGRAM_SESSION_STRING: str = ""
    TELEGRAM_PHONE: Optional[str] = None

    # Channel routing
    MAIN_FEED_CHANNEL_ID: Optional[int] = None
    TEST_FEED_CHANNEL_ID: Optional[int] = None

    # Debounce window (seconds); reduce to 0.1 in test suites
    DEBOUNCE_DELAY_SECONDS: float = 90.0

    # LLM inference (OpenAI-compatible endpoint)
    LLM_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    LLM_BASE_URL: Optional[str] = "https://api.groq.com/openai/v1"
    LLM_MODEL: str = "openai/gpt-oss-20b"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
