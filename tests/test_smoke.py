import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_index_page_renders_with_htmx():
    response = client.get("/")
    assert response.status_code == 200
    assert "htmx.org@2.0.11" in response.text


def test_static_css_is_served():
    response = client.get("/static/style.css")
    assert response.status_code == 200


def test_extractor_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("EXTRACTOR", raising=False)
    assert Settings(_env_file=None).extractor == "mock"


def test_invalid_extractor_fails_at_startup(monkeypatch):
    monkeypatch.setenv("EXTRACTOR", "banana")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
