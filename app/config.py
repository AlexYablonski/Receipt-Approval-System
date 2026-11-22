from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Real environment variables win over values in a local .env file.
    # Each field is read from the env var with the same name, upper-cased (extractor -> EXTRACTOR).
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # An invalid value (e.g. EXTRACTOR=banana) stops the app at startup with a clear error.
    extractor: Literal["mock", "ollama"] = "mock"

    # A relative SQLite path is relative to where the app starts (the repo root in venv mode,
    # /app in Docker), so the database lands in the ./data folder either way.
    database_url: str = "sqlite:///data/app.db"


settings = Settings()
