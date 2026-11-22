"""Schema tests. Each test builds a fresh temporary SQLite file by running the real Alembic migrations,
so these tests check the migration and the models together."""

import threading
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import create_db_engine
from app.models import (
    AuditEvent,
    Category,
    Decision,
    LineItem,
    Receipt,
    ReceiptStatus,
    Review,
    Role,
    User,
)

ROOT = Path(__file__).resolve().parents[1]


def alembic_config(db_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", db_url)
    return config


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


# --- migrations ---------------------------------------------------------------


def test_upgrade_creates_all_tables(engine):
    tables = set(inspect(engine).get_table_names())
    assert tables == {"users", "categories", "receipts", "line_items", "reviews", "audit_events", "alembic_version"}


def test_models_and_migration_agree(db_url, engine):
    # "alembic check" fails if the models have changes that no migration covers yet.
    command.check(alembic_config(db_url))


def test_downgrade_removes_everything(db_url, engine):
    command.downgrade(alembic_config(db_url), "base")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}


def test_first_migration_seeds_categories(session):
    names = [category.name for category in session.query(Category).order_by(Category.id)]
    assert names == ["Meals", "Travel", "Lodging", "Office supplies", "Other"]


def test_expected_indexes_exist(engine):
    inspector = inspect(engine)

    def indexed_columns(table):
        return {tuple(index["column_names"]) for index in inspector.get_indexes(table)}

    assert {("owner_id",), ("status",), ("file_hash",)} <= indexed_columns("receipts")
    assert ("receipt_id",) in indexed_columns("line_items")
    assert ("receipt_id",) in indexed_columns("reviews")
    assert ("receipt_id", "created_at") in indexed_columns("audit_events")


# --- foreign keys -------------------------------------------------------------


def test_foreign_keys_are_on_for_every_connection(engine):
    for _ in range(2):
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_receipt_with_unknown_owner_is_rejected(session):
    session.add(
        Receipt(
            owner_id=999,
            status=ReceiptStatus.UPLOADED,
            generated_filename="x.pdf",
            original_filename="x.pdf",
            file_hash="b" * 64,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_cannot_delete_a_user_who_owns_receipts(session):
    owner = make_user(session)
    make_receipt(session, owner)
    session.delete(owner)
    with pytest.raises(IntegrityError):
        session.commit()


def test_line_item_category_must_exist(session):
    receipt = make_receipt(session, make_user(session))
    session.add(LineItem(receipt_id=receipt.id, amount_cents=450, category_id=999))
    with pytest.raises(IntegrityError):
        session.commit()


# --- constraints --------------------------------------------------------------


def test_unknown_status_is_rejected_by_the_database(engine, session):
    owner = make_user(session)
    # Raw SQL on purpose: the ORM would refuse a bad value before the database ever saw it.
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO receipts (owner_id, created_at, status, generated_filename, original_filename,"
                    " file_hash, updated_at) VALUES (:owner, '2026-01-01', 'BANANA', 'x', 'x', 'h', '2026-01-01')"
                ),
                {"owner": owner.id},
            )


def test_unknown_role_is_rejected_by_the_database(engine):
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO users (email, name, role, password_hash) VALUES ('a@b.c', 'A', 'admin', 'h')")
            )


def test_email_must_be_unique(session):
    make_user(session, email="same@example.com")
    session.add(User(email="same@example.com", name="Other", role=Role.MANAGER, password_hash="h"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_duplicate_file_hash_is_allowed(session):
    # Duplicates are flagged by the app, never blocked by the database.
    owner = make_user(session)
    make_receipt(session, owner, file_hash="c" * 64)
    make_receipt(session, owner, file_hash="c" * 64, generated_filename="def456.pdf")
    assert session.query(Receipt).filter_by(file_hash="c" * 64).count() == 2


def test_rejection_requires_a_comment(session):
    owner = make_user(session)
    manager = make_user(session, email="mgr@example.com", role=Role.MANAGER)
    receipt = make_receipt(session, owner)

    for bad_comment in (None, "", "   "):
        session.add(Review(receipt_id=receipt.id, manager_id=manager.id, decision=Decision.REJECTED, comment=bad_comment))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    session.add(Review(receipt_id=receipt.id, manager_id=manager.id, decision=Decision.REJECTED, comment="Missing itemized receipt"))
    session.add(Review(receipt_id=receipt.id, manager_id=manager.id, decision=Decision.APPROVED))
    session.commit()
    assert session.query(Review).count() == 2


# --- round trips --------------------------------------------------------------


def test_new_receipt_defaults_to_uploaded(session):
    owner = make_user(session)
    receipt = Receipt(owner_id=owner.id, generated_filename="x.pdf", original_filename="x.pdf", file_hash="d" * 64)
    session.add(receipt)
    session.commit()
    assert receipt.status is ReceiptStatus.UPLOADED


def test_receipt_needs_only_the_upload_columns(session):
    receipt = make_receipt(session, make_user(session))
    session.refresh(receipt)
    for column in ("purchase_date", "total_cents", "merchant", "currency", "tax_cents", "description",
                   "extracted_data", "confidence", "failure_reason"):
        assert getattr(receipt, column) is None
    assert receipt.created_at is not None
    assert receipt.updated_at is not None


def test_enums_are_stored_as_text_values(engine, session):
    owner = make_user(session)
    make_receipt(session, owner)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT role FROM users")).scalar() == "employee"
        assert connection.execute(text("SELECT status FROM receipts")).scalar() == "UPLOADED"
    session.expire_all()
    assert session.query(User).one().role is Role.EMPLOYEE
    assert session.query(Receipt).one().status is ReceiptStatus.UPLOADED


def test_json_and_related_rows_round_trip(session):
    owner = make_user(session)
    receipt = make_receipt(session, owner, extracted_data={"merchant": "Cafe", "total": "4.50"})
    meals = session.query(Category).filter_by(name="Meals").one()
    receipt.line_items.append(LineItem(amount_cents=450, category_id=meals.id))
    receipt.audit_events.append(
        AuditEvent(event_type="STATUS_CHANGED", from_status=ReceiptStatus.UPLOADED, to_status=ReceiptStatus.PROCESSING)
    )
    session.commit()
    session.expire_all()

    saved = session.query(Receipt).one()
    assert saved.extracted_data == {"merchant": "Cafe", "total": "4.50"}
    assert saved.line_items[0].category.name == "Meals"
    event = saved.audit_events[0]
    assert event.actor_id is None  # system action, no human actor
    assert (event.from_status, event.to_status) == ("UPLOADED", "PROCESSING")


def test_updated_at_changes_on_update(session):
    receipt = make_receipt(session, make_user(session))
    first = receipt.updated_at
    receipt.merchant = "Corner Cafe"
    session.commit()
    assert receipt.updated_at > first


# --- threading ----------------------------------------------------------------


def test_connection_can_be_used_from_another_thread(engine):
    result = []

    def worker():
        with Session(engine) as session:
            result.append(session.query(Category).count())

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert result == [5]
