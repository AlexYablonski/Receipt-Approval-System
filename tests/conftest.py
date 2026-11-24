"""Fixtures shared by the test files. Each test gets a fresh temporary SQLite file built by running
the real Alembic migrations."""

import pytest
from alembic import command
from sqlalchemy.orm import Session

from app.db import create_db_engine
from tests.helpers import alembic_config


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def engine(db_url):
    command.upgrade(alembic_config(db_url), "head")
    engine = create_db_engine(db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session
