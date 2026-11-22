"""Picks the extractor named by the EXTRACTOR env var."""

from app.config import settings
from app.services.extractors.base import Extractor
from app.services.extractors.mock import MockExtractor


def get_extractor() -> Extractor:
    if settings.extractor == "mock":
        return MockExtractor(
            delay_seconds=settings.mock_delay_seconds,
            fail=settings.mock_fail,
        )
    # Settings only allows "mock" or "ollama", so this is the ollama case.
    raise NotImplementedError("The ollama extractor is not built yet (module 9).")
