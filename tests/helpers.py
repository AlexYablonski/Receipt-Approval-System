"""Plain helper functions shared by the test files. The fixtures that use them are in conftest.py."""

from pathlib import Path

from alembic.config import Config

from app.models import Receipt, ReceiptStatus, Role, User

ROOT = Path(__file__).resolve().parents[1]


def alembic_config(db_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", db_url)
    return config


def make_user(session, email="emp@example.com", role=Role.EMPLOYEE):
    user = User(email=email, name="Test User", role=role, password_hash="not-a-real-hash")
    session.add(user)
    session.commit()
    return user


def make_receipt(session, owner, **overrides):
    fields = dict(
        owner_id=owner.id,
        status=ReceiptStatus.UPLOADED,
        generated_filename="abc123.pdf",
        original_filename="lunch.pdf",
        file_hash="a" * 64,
    )
    fields.update(overrides)
    receipt = Receipt(**fields)
    session.add(receipt)
    session.commit()
    return receipt
