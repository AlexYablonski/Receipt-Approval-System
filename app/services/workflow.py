"""The status workflow: the ONLY place a receipt's status changes (project rule 7).

TRANSITIONS lists every allowed move and who may make it. Every move writes an audit event.
Workflow functions add rows to the session but never commit, so the caller commits once.
The one exception is process_receipt, the background task: it is its own caller and commits at fixed points.
"""

import enum
import logging
import re
from collections.abc import Callable
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
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
    utcnow,
)
from app.services.extractors import get_extractor
from app.services.extractors.base import ExtractionError, ExtractionResult, Extractor

logger = logging.getLogger(__name__)

# Shown to the employee when extraction fails for any reason other than ExtractionError.
# The real error goes to the logs only.
GENERIC_FAILURE_REASON = "Extraction failed unexpectedly. You can retry or enter the details by hand."


# --- errors -------------------------------------------------------------------
# Raised before any row is written and never stored in the database. Separate from ExtractionError.


class WorkflowError(Exception):
    pass


class InvalidTransition(WorkflowError):
    """The receipt's current status does not allow this action (routers answer 409)."""


class PermissionDenied(WorkflowError):
    """This actor may not make this move on this receipt (routers answer 403)."""


class ValidationFailed(WorkflowError):
    """The data is not acceptable. `problems` lists everything wrong, so a form can show it all at once."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# --- the transition table -----------------------------------------------------


class Who(enum.StrEnum):
    SYSTEM = "system"  # the background task; the actor is None
    OWNER = "owner"  # the user who owns the receipt
    MANAGER = "manager"  # any user whose role is manager


class EventType(enum.StrEnum):
    # Plain text in audit_events.event_type (no CHECK constraint).
    RECEIPT_UPLOADED = "RECEIPT_UPLOADED"  # written by the upload code (module 5), not a transition
    EXTRACTION_STARTED = "EXTRACTION_STARTED"
    EXTRACTION_SUCCEEDED = "EXTRACTION_SUCCEEDED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    MANUAL_ENTRY_CHOSEN = "MANUAL_ENTRY_CHOSEN"
    RECEIPT_SUBMITTED = "RECEIPT_SUBMITTED"
    RECEIPT_APPROVED = "RECEIPT_APPROVED"
    RECEIPT_REJECTED = "RECEIPT_REJECTED"
    RECEIPT_REOPENED = "RECEIPT_REOPENED"


class Move(NamedTuple):
    who: Who
    event: EventType


S = ReceiptStatus

# Any (from, to) pair that is not in this table is refused. APPROVED has no way out.
TRANSITIONS: dict[tuple[ReceiptStatus, ReceiptStatus], Move] = {
    (S.UPLOADED, S.PROCESSING): Move(Who.SYSTEM, EventType.EXTRACTION_STARTED),
    (S.PROCESSING, S.REVIEW): Move(Who.SYSTEM, EventType.EXTRACTION_SUCCEEDED),
    (S.PROCESSING, S.FAILED): Move(Who.SYSTEM, EventType.EXTRACTION_FAILED),
    (S.FAILED, S.PROCESSING): Move(Who.OWNER, EventType.EXTRACTION_STARTED),  # retry
    (S.FAILED, S.REVIEW): Move(Who.OWNER, EventType.MANUAL_ENTRY_CHOSEN),
    (S.REVIEW, S.SUBMITTED): Move(Who.OWNER, EventType.RECEIPT_SUBMITTED),
    (S.SUBMITTED, S.APPROVED): Move(Who.MANAGER, EventType.RECEIPT_APPROVED),
    (S.SUBMITTED, S.REJECTED): Move(Who.MANAGER, EventType.RECEIPT_REJECTED),
    (S.REJECTED, S.REVIEW): Move(Who.OWNER, EventType.RECEIPT_REOPENED),
}


def _may_act(who: Who, receipt: Receipt, actor: User | None) -> bool:
    if who is Who.SYSTEM:
        return actor is None
    if actor is None:
        return False
    if who is Who.OWNER:
        return actor.id == receipt.owner_id
    return actor.role == Role.MANAGER


def _check_move(receipt: Receipt, to_status: ReceiptStatus, actor: User | None) -> Move:
    move = TRANSITIONS.get((receipt.status, to_status))
    if move is None:
        raise InvalidTransition(f"A receipt cannot move from {receipt.status} to {to_status}.")
    if not _may_act(move.who, receipt, actor):
        raise PermissionDenied(f"This user may not move receipt {receipt.id} from {receipt.status} to {to_status}.")
    return move


def _require_status(receipt: Receipt, expected: ReceiptStatus) -> None:
    # The table alone is not enough for the named functions: REVIEW can be reached from FAILED and from
    # REJECTED, so each function states the status it starts from.
    if receipt.status != expected:
        raise InvalidTransition(f"This action needs a receipt in {expected}, but it is {receipt.status}.")


def transition(
    session: Session,
    receipt: Receipt,
    to_status: ReceiptStatus,
    actor: User | None,
    detail: dict[str, Any] | None = None,
) -> AuditEvent:
    """The single place that writes a status and its audit event. The named functions below call this
    after their own checks. Nothing is committed."""
    move = _check_move(receipt, to_status, actor)
    from_status = receipt.status
    receipt.status = to_status
    event = AuditEvent(
        receipt_id=receipt.id,
        actor_id=actor.id if actor else None,
        event_type=str(move.event),
        from_status=str(from_status),
        to_status=str(to_status),
        detail=detail,
    )
    session.add(event)
    return event


# --- text and field rules -----------------------------------------------------


def _clean(value: str | None) -> str | None:
    """Trim the ends of a text value. A blank value counts as missing (None)."""
    if value is None:
        return None
    return value.strip() or None


CURRENCY_FORMAT = re.compile(r"[A-Za-z]{3}")  # ASCII only; stored upper-case, like the extractor's output
TEXT_FIELDS = {"merchant", "description"}
EDITABLE_FIELDS = TEXT_FIELDS | {"purchase_date", "total_cents", "currency", "tax_cents"}


def _line_items(session: Session, receipt: Receipt) -> list[LineItem]:
    return list(session.scalars(select(LineItem).where(LineItem.receipt_id == receipt.id).order_by(LineItem.id)))


def _require_editable(receipt: Receipt, actor: User | None) -> None:
    _require_status(receipt, ReceiptStatus.REVIEW)
    if actor is None or actor.id != receipt.owner_id:
        raise PermissionDenied(f"Only the owner may edit receipt {receipt.id}.")


def update_receipt(session: Session, receipt: Receipt, actor: User | None, **fields: Any) -> None:
    """Save edits to the receipt's own fields (REVIEW only, owner only). Writes no audit event.

    Text is trimmed and a blank value is saved as NULL; "required" is only enforced at submit. The currency
    is upper-cased and must be three letters. Raises ValidationFailed, with nothing changed, if it is not."""
    _require_editable(receipt, actor)
    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"Unknown receipt fields: {sorted(unknown)}")

    cleaned: dict[str, Any] = {}
    problems: list[str] = []
    for name, value in fields.items():
        if name in TEXT_FIELDS:
            value = _clean(value)
        elif name == "currency":
            value = _clean(value)
            if value is not None:
                if CURRENCY_FORMAT.fullmatch(value):
                    value = value.upper()
                else:
                    problems.append("Currency must be a three-letter code such as USD.")
        cleaned[name] = value
    if problems:
        raise ValidationFailed(problems)

    for name, value in cleaned.items():
        setattr(receipt, name, value)


def set_line_items(
    session: Session, receipt: Receipt, actor: User | None, items: list[tuple[int, int | None]]
) -> None:
    """Replace all of the receipt's line items with `items`, a list of (amount_cents, category_id)
    (REVIEW only, owner only). Writes no audit event."""
    _require_editable(receipt, actor)
    for old in _line_items(session, receipt):
        session.delete(old)
    for amount_cents, category_id in items:
        session.add(LineItem(receipt_id=receipt.id, amount_cents=amount_cents, category_id=category_id))
    session.flush()
    session.expire(receipt, ["line_items"])  # the loaded list is out of date now


# --- moves made by the employee ----------------------------------------------


def retry(session: Session, receipt: Receipt, actor: User | None) -> None:
    """FAILED -> PROCESSING. The caller commits, then starts process_receipt (a background task)."""
    _require_status(receipt, ReceiptStatus.FAILED)
    _check_move(receipt, ReceiptStatus.PROCESSING, actor)
    receipt.failure_reason = None  # the audit history keeps the old reason
    transition(session, receipt, ReceiptStatus.PROCESSING, actor, {"extractor": settings.extractor})


def choose_manual_entry(session: Session, receipt: Receipt, actor: User | None) -> None:
    """FAILED -> REVIEW. The receipt has no extracted data, so the employee fills everything in."""
    _require_status(receipt, ReceiptStatus.FAILED)
    transition(session, receipt, ReceiptStatus.REVIEW, actor)


def _submit_problems(receipt: Receipt, items: list[LineItem]) -> list[str]:
    problems = []
    if _clean(receipt.merchant) is None:
        problems.append("Merchant is required.")
    if receipt.purchase_date is None:
        problems.append("Purchase date is required.")
    elif receipt.purchase_date > utcnow().date():
        problems.append("Purchase date cannot be in the future.")
    if receipt.total_cents is None:
        problems.append("Total is required.")
    elif receipt.total_cents <= 0:
        problems.append("Total must be greater than zero.")
    if _clean(receipt.currency) is None:
        problems.append("Currency is required.")
    if receipt.tax_cents is not None and receipt.tax_cents < 0:
        problems.append("Tax cannot be negative.")
    if not items:
        problems.append("At least one line item is required.")
    for number, item in enumerate(items, start=1):
        if item.amount_cents <= 0:
            problems.append(f"Line item {number}: amount must be greater than zero.")
        if item.category_id is None:
            problems.append(f"Line item {number}: choose a category.")
    return problems


def _totals_mismatch(receipt: Receipt, items: list[LineItem]) -> dict[str, int] | None:
    items_plus_tax = sum(item.amount_cents for item in items) + (receipt.tax_cents or 0)
    if items_plus_tax == receipt.total_cents:
        return None
    return {
        "line_items_plus_tax_cents": items_plus_tax,
        "total_cents": receipt.total_cents,
        "difference_cents": receipt.total_cents - items_plus_tax,
    }


def submit(session: Session, receipt: Receipt, actor: User | None) -> None:
    """REVIEW -> SUBMITTED. Raises ValidationFailed, listing every problem, if the receipt is incomplete.
    A totals mismatch does not block submission; it is recorded in the audit event for the manager."""
    _require_status(receipt, ReceiptStatus.REVIEW)
    _check_move(receipt, ReceiptStatus.SUBMITTED, actor)
    items = _line_items(session, receipt)
    problems = _submit_problems(receipt, items)
    if problems:
        raise ValidationFailed(problems)
    transition(
        session, receipt, ReceiptStatus.SUBMITTED, actor, {"totals_mismatch": _totals_mismatch(receipt, items)}
    )


def reopen(session: Session, receipt: Receipt, actor: User | None) -> None:
    """REJECTED -> REVIEW, so the employee can edit and resubmit."""
    _require_status(receipt, ReceiptStatus.REJECTED)
    _check_move(receipt, ReceiptStatus.REVIEW, actor)
    review_id = session.scalar(
        select(Review.id)
        .where(Review.receipt_id == receipt.id, Review.decision == Decision.REJECTED)
        .order_by(Review.id.desc())
        .limit(1)
    )
    transition(session, receipt, ReceiptStatus.REVIEW, actor, {"review_id": review_id})


# --- moves made by a manager --------------------------------------------------


def approve(session: Session, receipt: Receipt, actor: User | None, comment: str | None = None) -> Review:
    """SUBMITTED -> APPROVED. The comment is optional."""
    _require_status(receipt, ReceiptStatus.SUBMITTED)
    _check_move(receipt, ReceiptStatus.APPROVED, actor)
    review = Review(receipt_id=receipt.id, manager_id=actor.id, decision=Decision.APPROVED, comment=_clean(comment))
    session.add(review)
    session.flush()  # the audit event needs the review's id
    transition(session, receipt, ReceiptStatus.APPROVED, actor, {"review_id": review.id})
    return review


def reject(session: Session, receipt: Receipt, actor: User | None, comment: str | None) -> Review:
    """SUBMITTED -> REJECTED. A comment that is blank after trimming raises ValidationFailed."""
    _require_status(receipt, ReceiptStatus.SUBMITTED)
    _check_move(receipt, ReceiptStatus.REJECTED, actor)
    comment = _clean(comment)
    if comment is None:
        raise ValidationFailed(["A comment is required when rejecting a receipt."])
    review = Review(receipt_id=receipt.id, manager_id=actor.id, decision=Decision.REJECTED, comment=comment)
    session.add(review)
    session.flush()
    transition(session, receipt, ReceiptStatus.REJECTED, actor, {"review_id": review.id})
    return review


# --- the background process ---------------------------------------------------


def _apply_extraction_result(session: Session, receipt: Receipt, result: ExtractionResult) -> None:
    receipt.extracted_data = result.model_dump(mode="json")  # raw output, never edited afterwards
    receipt.merchant = _clean(result.merchant)
    receipt.purchase_date = result.purchase_date
    receipt.total_cents = result.total_cents
    receipt.currency = result.currency
    receipt.tax_cents = result.tax_cents
    receipt.confidence = result.confidence
    # A category name that is not in the company list leaves the category empty for the employee to choose.
    category_ids = {category.name: category.id for category in session.scalars(select(Category))}
    for item in result.line_items:
        session.add(
            LineItem(
                receipt_id=receipt.id,
                amount_cents=item.amount_cents,
                category_id=category_ids.get(item.category),
            )
        )


def _record_failure(session: Session, receipt_id: int, reason: str, kind: str) -> None:
    session.rollback()  # discard anything half-written, which also makes the session usable again
    receipt = session.get(Receipt, receipt_id)
    receipt.failure_reason = reason
    transition(session, receipt, ReceiptStatus.FAILED, None, {"reason": reason, "kind": kind})
    session.commit()


def process_receipt(
    receipt_id: int,
    extractor: Extractor | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    """Run the extractor for one receipt. Meant to run as a background task, after the request has
    finished, so it opens its own session. `extractor` and `session_factory` can be passed in by tests.

    The receipt must be UPLOADED (first run) or PROCESSING (after retry() was committed); anything else
    is logged and left alone. Commits: (1) PROCESSING + audit event, so the UI shows it; then either
    (2) result + REVIEW + audit event, or (3) FAILED + audit event.

    If recording FAILED itself fails (for example the database is down), the exception propagates and
    the receipt stays in PROCESSING. A stale-state sweeper would fix that (see README, Future improvements).
    """
    with session_factory() as session:
        receipt = session.get(Receipt, receipt_id)
        if receipt is None:
            logger.error("process_receipt: receipt %s does not exist", receipt_id)
            return
        if receipt.status == ReceiptStatus.UPLOADED:
            transition(session, receipt, ReceiptStatus.PROCESSING, None, {"extractor": settings.extractor})
            session.commit()
        elif receipt.status != ReceiptStatus.PROCESSING:
            logger.warning("process_receipt: receipt %s is %s, nothing to do", receipt_id, receipt.status)
            return

        try:
            # Inside the try so that a failure to build the extractor (for example EXTRACTOR=ollama before
            # module 9) also ends in FAILED instead of leaving the receipt in PROCESSING.
            active = extractor if extractor is not None else get_extractor()
            categories = list(session.scalars(select(Category.name).order_by(Category.id)))
            result = active.extract(settings.upload_dir / receipt.generated_filename, categories)
            _apply_extraction_result(session, receipt, result)
            transition(
                session,
                receipt,
                ReceiptStatus.REVIEW,
                None,
                {"extractor": settings.extractor, "line_item_count": len(result.line_items)},
            )
            session.commit()
        except ExtractionError as error:
            # Extractor messages are written to be shown to the employee.
            _record_failure(session, receipt_id, str(error) or GENERIC_FAILURE_REASON, "extraction_error")
        except Exception:  # not BaseException, so shutdown signals still work
            logger.exception("Extraction failed unexpectedly for receipt %s", receipt_id)
            _record_failure(session, receipt_id, GENERIC_FAILURE_REASON, "unexpected")
