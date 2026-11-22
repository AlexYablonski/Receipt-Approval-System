"""Mock extractor: invents a plausible receipt from the file's bytes. No AI involved.

The same bytes always give the same result, so tests and demos are repeatable.
"""

import hashlib
import random
import time
from datetime import date, timedelta
from pathlib import Path

from app.services.extractors.base import (
    ExtractedLineItem,
    ExtractionError,
    ExtractionResult,
    Extractor,
)

MERCHANTS = [
    "Blue Door Cafe",
    "City Cab Co.",
    "Harbor Hotel",
    "Paper & Pen Supply",
    "Greenfield Market",
    "Skyline Airlines",
    "Union Station Diner",
    "QuickPrint Shop",
]
# Two-decimal currencies only (a known limitation of the integer cents columns).
CURRENCIES = ["USD", "EUR", "GBP", "CAD"]
# None means the receipt shows no tax.
TAX_RATES_PERCENT = [None, 5, 8, 10]


def _score(rng: random.Random) -> float:
    return round(rng.uniform(0.5, 0.99), 2)


class MockExtractor(Extractor):
    def __init__(self, delay_seconds: float, fail: bool) -> None:
        self.delay_seconds = delay_seconds
        self.fail = fail

    def extract(self, file_path: Path, categories: list[str]) -> ExtractionResult:
        # Wait first, like a real AI call would, so PROCESSING is visible even when failing.
        time.sleep(self.delay_seconds)
        if self.fail:
            raise ExtractionError("Mock extractor failure (MOCK_FAIL is true).")

        try:
            data = file_path.read_bytes()
        except OSError as exc:
            # Generic message on purpose: failure_reason may be shown to the employee.
            raise ExtractionError("Could not read the uploaded file.") from exc

        # Seeding a random generator with the file's SHA-256 makes its "random" picks repeatable.
        rng = random.Random(int.from_bytes(hashlib.sha256(data).digest(), "big"))

        merchant = rng.choice(MERCHANTS)
        # Fixed start date (not today) so the output never changes from one day to the next.
        purchase_date = date(2026, 1, 1) + timedelta(days=rng.randrange(300))
        currency = rng.choice(CURRENCIES)

        amounts = [rng.randint(300, 9500) for _ in range(rng.randint(1, 4))]
        subtotal = sum(amounts)
        rate = rng.choice(TAX_RATES_PERCENT)
        tax_cents = None if rate is None else subtotal * rate // 100
        # Total is built from the items plus tax, so "items + tax = total" always holds.
        total_cents = subtotal + (tax_cents or 0)

        line_items = []
        item_scores = []
        for amount in amounts:
            category = rng.choice(categories) if categories else None
            line_items.append(ExtractedLineItem(amount_cents=amount, category=category))
            scores = {"amount": _score(rng)}
            if category is not None:
                scores["category"] = _score(rng)
            item_scores.append(scores)

        confidence = {
            "merchant": _score(rng),
            "purchase_date": _score(rng),
            "total": _score(rng),
            "currency": _score(rng),
        }
        if tax_cents is not None:
            confidence["tax"] = _score(rng)
        confidence["line_items"] = item_scores

        return ExtractionResult(
            merchant=merchant,
            purchase_date=purchase_date,
            total_cents=total_cents,
            currency=currency,
            tax_cents=tax_cents,
            line_items=line_items,
            confidence=confidence,
        )
