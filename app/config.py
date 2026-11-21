from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Real environment variables win over values in a local .env file.
    # Each field is read from the env var with the same name, upper-cased (extractor -> EXTRACTOR).
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # An invalid value (e.g. EXTRACTOR=banana) stops the app at startup with a clear error.
    extractor: Literal["mock", "ollama"] = "mock"


settings = Settings()
