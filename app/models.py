"""Database tables (SQLAlchemy 2.0 declarative models). The design lives in DB_SCHEMA.txt."""

import enum
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Every constraint and index gets a predictable name. Alembic's batch mode (needed for SQLite)
# can only drop or change a constraint it can refer to by name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    # SQLite stores no timezone, so we store naive datetimes that are always UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Role(enum.StrEnum):
    EMPLOYEE = "employee"
    MANAGER = "manager"


class ReceiptStatus(enum.StrEnum):
    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    REVIEW = "REVIEW"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"  # extraction failed; retryable (our addition to the take-home's list)


class Decision(enum.StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


def enum_column(enum_class: type[enum.StrEnum], constraint_name: str) -> Enum:
    # Stored as plain text plus a CHECK constraint (SQLite has no real enum type, and this
    # also works on Postgres). values_callable stores the values ("employee"), not the names.
    return Enum(
        enum_class,
        name=constraint_name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda members: [member.value for member in members],
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    role: Mapped[Role] = mapped_column(enum_column(Role, "role_values"))
    password_hash: Mapped[str] = mapped_column(String)


class Category(Base):
    """Company-defined cost buckets. The first list is inserted by the initial migration."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # New receipts start as UPLOADED. This default is applied by SQLAlchemy when a row is created,
    # it is not stored in the database. Every later change still goes through services/workflow.py.
    status: Mapped[ReceiptStatus] = mapped_column(
        enum_column(ReceiptStatus, "status_values"), default=ReceiptStatus.UPLOADED, index=True
    )

    # Nullable because the row exists from upload, before extraction fills these in.
    # "Required to submit" is enforced in services/workflow.py, not in the schema.
    purchase_date: Mapped[date | None]
    total_cents: Mapped[int | None]
    merchant: Mapped[str | None] = mapped_column(String)
    currency: Mapped[str | None] = mapped_column(String(3))
    tax_cents: Mapped[int | None]
    description: Mapped[str | None] = mapped_column(Text)  # entered by the employee, not extracted

    extracted_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # raw extractor output, never edited
    confidence: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    generated_filename: Mapped[str] = mapped_column(String)  # name on disk under data/uploads/
    original_filename: Mapped[str] = mapped_column(String)  # as the user supplied it; display only
    file_hash: Mapped[str] = mapped_column(String(64), index=True)  # SHA-256 hex; not unique, duplicates are flagged

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    owner: Mapped["User"] = relationship()
    line_items: Mapped[list["LineItem"]] = relationship(back_populates="receipt")
    reviews: Mapped[list["Review"]] = relationship(back_populates="receipt")
    audit_events: Mapped[list["AuditEvent"]] = relationship(back_populates="receipt")


class LineItem(Base):
    __tablename__ = "line_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id"), index=True)
    amount_cents: Mapped[int]
    # Nullable: the extractor creates line items before the employee picks a category.
    # "Every line item needs a category before submit" is a workflow rule.
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))

    receipt: Mapped["Receipt"] = relationship(back_populates="line_items")
    category: Mapped["Category | None"] = relationship()


class Review(Base):
    """One row per manager decision, so earlier rejection comments are never overwritten."""

    __tablename__ = "reviews"
    __table_args__ = (
        # The IS NOT NULL part matters: a CHECK passes when its result is NULL, so
        # length(trim(NULL)) > 0 on its own would let a rejection without a comment through.
        # SQLite's trim() strips spaces only; the workflow layer should strip other whitespace.
        CheckConstraint(
            "decision = 'approved' OR (comment IS NOT NULL AND length(trim(comment)) > 0)",
            name="rejection_needs_comment",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id"), index=True)
    manager_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[Decision] = mapped_column(enum_column(Decision, "decision_values"))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    receipt: Mapped["Receipt"] = relationship(back_populates="reviews")
    manager: Mapped["User"] = relationship()


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_receipt_id_created_at", "receipt_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id"))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))  # NULL = system / background task
    event_type: Mapped[str] = mapped_column(String)
    # Plain text with no CHECK, so old history stays valid if the status list ever changes.
    from_status: Mapped[str | None] = mapped_column(String)
    to_status: Mapped[str | None] = mapped_column(String)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    receipt: Mapped["Receipt"] = relationship(back_populates="audit_events")
    actor: Mapped["User | None"] = relationship()
