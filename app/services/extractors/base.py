"""The extractor interface: what every extractor accepts and what it returns.

Extractors only read a file and return data. They never touch the database; the workflow
saves the result (receipts.extracted_data holds result.model_dump(mode="json")).
"""

from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field


class ExtractionError(Exception):
    """The receipt could not be read. The message is stored as receipts.failure_reason."""


class ExtractedLineItem(BaseModel):
    amount_cents: int
    # A name from the categories list given to extract(), or None if there is no guess.
    category: str | None = None


class ExtractionResult(BaseModel):
    # Every field is optional because an extractor may not find everything on a receipt.
    merchant: str | None = None
    purchase_date: date | None = None
    total_cents: int | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")  # ISO 4217 code
    tax_cents: int | None = None
    line_items: list[ExtractedLineItem] = Field(default_factory=list)
    # Scores from 0.0 to 1.0 per field, plus a "line_items" list with one entry per item.
    # Stored as-is in receipts.confidence (a JSON column).
    confidence: dict = Field(default_factory=dict)


class Extractor(ABC):
    @abstractmethod
    def extract(self, file_path: Path, categories: list[str]) -> ExtractionResult:
        """Read the receipt at file_path and guess a category for each line item from
        `categories`. Raise ExtractionError if the receipt cannot be read."""
