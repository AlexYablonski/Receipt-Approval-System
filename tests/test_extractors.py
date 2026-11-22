import json

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.services.extractors import get_extractor
from app.services.extractors import mock as mock_module
from app.services.extractors.base import ExtractionError, ExtractionResult, Extractor
from app.services.extractors.mock import MockExtractor

CATEGORIES = ["Meals", "Travel", "Lodging", "Office supplies", "Other"]


@pytest.fixture
def extractor():
    return MockExtractor(delay_seconds=0, fail=False)


def make_file(tmp_path, content: bytes, name: str = "receipt.bin"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_same_bytes_give_same_result(extractor, tmp_path):
    first = make_file(tmp_path, b"receipt one", "a.bin")
    copy = make_file(tmp_path, b"receipt one", "b.bin")  # different name, same bytes
    assert extractor.extract(first, CATEGORIES) == extractor.extract(first, CATEGORIES)
    assert extractor.extract(first, CATEGORIES) == extractor.extract(copy, CATEGORIES)


def test_different_bytes_give_different_results(extractor, tmp_path):
    one = extractor.extract(make_file(tmp_path, b"receipt one", "a.bin"), CATEGORIES)
    two = extractor.extract(make_file(tmp_path, b"receipt two", "b.bin"), CATEGORIES)
    assert one != two


def test_result_is_fully_populated(extractor, tmp_path):
    result = extractor.extract(make_file(tmp_path, b"x"), CATEGORIES)
    assert result.merchant
    assert result.purchase_date
    assert result.total_cents > 0
    assert len(result.currency) == 3
    assert len(result.line_items) >= 1

    scores = result.confidence
    assert len(scores["line_items"]) == len(result.line_items)
    top_level = [value for key, value in scores.items() if key != "line_items"]
    per_item = [value for item in scores["line_items"] for value in item.values()]
    assert all(0.0 <= score <= 1.0 for score in top_level + per_item)


def test_line_items_plus_tax_equal_total(extractor, tmp_path):
    results = [
        extractor.extract(make_file(tmp_path, f"receipt {i}".encode(), f"{i}.bin"), CATEGORIES)
        for i in range(200)
    ]
    for result in results:
        items_total = sum(item.amount_cents for item in result.line_items)
        assert len(result.line_items) >= 1
        assert items_total + (result.tax_cents or 0) == result.total_cents

    # The mock covers receipts with and without tax, and different numbers of items.
    assert any(r.tax_cents is None for r in results)
    assert any(r.tax_cents is not None for r in results)
    assert len({len(r.line_items) for r in results}) > 1


def test_categories_come_from_the_list_given(extractor, tmp_path):
    result = extractor.extract(make_file(tmp_path, b"x"), ["Meals", "Travel"])
    assert all(item.category in ("Meals", "Travel") for item in result.line_items)
    assert all("category" in scores for scores in result.confidence["line_items"])


def test_no_categories_given_means_no_category_guess(extractor, tmp_path):
    result = extractor.extract(make_file(tmp_path, b"x"), [])
    assert all(item.category is None for item in result.line_items)
    assert all("category" not in scores for scores in result.confidence["line_items"])


def test_fail_flag_raises_extraction_error(tmp_path):
    failing = MockExtractor(delay_seconds=0, fail=True)
    with pytest.raises(ExtractionError):
        failing.extract(make_file(tmp_path, b"x"), CATEGORIES)


def test_unreadable_file_raises_extraction_error(extractor, tmp_path):
    with pytest.raises(ExtractionError):
        extractor.extract(tmp_path / "missing.bin", CATEGORIES)


def test_delay_is_applied(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(mock_module.time, "sleep", sleeps.append)
    MockExtractor(delay_seconds=0.25, fail=False).extract(make_file(tmp_path, b"x"), CATEGORIES)
    assert sleeps == [0.25]


def test_result_can_be_stored_as_json_and_read_back(extractor, tmp_path):
    result = extractor.extract(make_file(tmp_path, b"x"), CATEGORIES)
    stored = json.loads(json.dumps(result.model_dump(mode="json")))
    assert stored["purchase_date"] == result.purchase_date.isoformat()
    assert ExtractionResult(**stored) == result


@pytest.mark.parametrize("code", ["usd", "US", "USDX", "12$"])
def test_currency_must_be_three_capital_letters(code):
    with pytest.raises(ValidationError):
        ExtractionResult(currency=code)


def test_interface_cannot_be_used_directly():
    with pytest.raises(TypeError):
        Extractor()


def test_factory_returns_the_mock(monkeypatch):
    monkeypatch.setattr(settings, "extractor", "mock")
    assert isinstance(get_extractor(), MockExtractor)


def test_factory_passes_the_mock_settings_through(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "extractor", "mock")
    monkeypatch.setattr(settings, "mock_delay_seconds", 0)
    monkeypatch.setattr(settings, "mock_fail", True)
    with pytest.raises(ExtractionError):
        get_extractor().extract(make_file(tmp_path, b"x"), CATEGORIES)


def test_factory_ollama_is_not_built_yet(monkeypatch):
    monkeypatch.setattr(settings, "extractor", "ollama")
    with pytest.raises(NotImplementedError):
        get_extractor()


def test_mock_settings_defaults(monkeypatch):
    monkeypatch.delenv("MOCK_FAIL", raising=False)
    monkeypatch.delenv("MOCK_DELAY_SECONDS", raising=False)
    fresh = Settings(_env_file=None)
    assert fresh.mock_fail is False
    assert fresh.mock_delay_seconds == 2.0


def test_mock_fail_env_var_is_read(monkeypatch):
    monkeypatch.setenv("MOCK_FAIL", "true")
    assert Settings(_env_file=None).mock_fail is True
