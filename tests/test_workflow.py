"""Workflow tests: the transition table, who may make each move, submit validation, decisions, editing
and the background extraction process. Each test gets a fresh temporary database (see conftest.py)."""

import logging
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import AuditEvent, Category, LineItem, ReceiptStatus as S, Review, Role
from app.services import workflow
from app.services.extractors.base import ExtractedLineItem, ExtractionError, ExtractionResult, Extractor
from app.services.extractors.mock import MockExtractor
from app.services.workflow import (
    GENERIC_FAILURE_REASON,
    InvalidTransition,
    PermissionDenied,
    ValidationFailed,
    approve,
    choose_manual_entry,
    process_receipt,
    reject,
    reopen,
    retry,
    set_line_items,
    submit,
    transition,
    update_receipt,
)
from tests.helpers import make_receipt, make_user


# --- test extractors (used only by tests; the mock cannot raise arbitrary errors) ---------------


class RaisingExtractor(Extractor):
    def __init__(self, error: Exception):
        self.error = error

    def extract(self, file_path, categories):
        raise self.error


class FixedExtractor(Extractor):
    def __init__(self, result: ExtractionResult):
        self.result = result

    def extract(self, file_path, categories):
        return self.result


# --- fixtures and helpers -----------------------------------------------------


@pytest.fixture
def owner(session):
    return make_user(session, email="owner@example.com")


@pytest.fixture
def other_employee(session):
    return make_user(session, email="other@example.com")


@pytest.fixture
def manager(session):
    return make_user(session, email="manager@example.com", role=Role.MANAGER)


@pytest.fixture
def actors(owner, other_employee, manager):
    return {"system": None, "owner": owner, "other_employee": other_employee, "manager": manager}


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    folder = tmp_path / "uploads"
    folder.mkdir()
    monkeypatch.setattr(settings, "upload_dir", folder)
    return folder


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine)


def audit_events(session, receipt):
    return list(session.scalars(select(AuditEvent).where(AuditEvent.receipt_id == receipt.id).order_by(AuditEvent.id)))


def line_items(session, receipt):
    return list(session.scalars(select(LineItem).where(LineItem.receipt_id == receipt.id).order_by(LineItem.id)))


def meals_id(session):
    return session.scalar(select(Category.id).where(Category.name == "Meals"))


def make_review_receipt(session, owner, items=None, **overrides):
    """A complete receipt in REVIEW that passes every submit rule. `items` is a list of
    (amount_cents, category) where category is "meals" or None."""
    items = [(900, "meals")] if items is None else items
    fields = dict(
        status=S.REVIEW,
        merchant="Corner Cafe",
        purchase_date=date(2026, 1, 15),
        total_cents=1000,
        currency="USD",
        tax_cents=100,
    )
    fields.update(overrides)
    receipt = make_receipt(session, owner, **fields)
    for amount_cents, category in items:
        session.add(
            LineItem(
                receipt_id=receipt.id,
                amount_cents=amount_cents,
                category_id=meals_id(session) if category == "meals" else None,
            )
        )
    session.commit()
    return receipt


def make_uploaded_receipt(session, owner, uploads, name="r1.bin", content=b"receipt bytes", **overrides):
    (uploads / name).write_bytes(content)
    return make_receipt(session, owner, generated_filename=name, **overrides)


# --- the transition table -----------------------------------------------------

# Written out here on purpose, separately from TRANSITIONS, so a mistake in the table fails a test.
ALLOWED_MOVES = [
    # (from, to, who, audit event type)
    (S.UPLOADED, S.PROCESSING, "system", "EXTRACTION_STARTED"),
    (S.PROCESSING, S.REVIEW, "system", "EXTRACTION_SUCCEEDED"),
    (S.PROCESSING, S.FAILED, "system", "EXTRACTION_FAILED"),
    (S.FAILED, S.PROCESSING, "owner", "EXTRACTION_STARTED"),
    (S.FAILED, S.REVIEW, "owner", "MANUAL_ENTRY_CHOSEN"),
    (S.REVIEW, S.SUBMITTED, "owner", "RECEIPT_SUBMITTED"),
    (S.SUBMITTED, S.APPROVED, "manager", "RECEIPT_APPROVED"),
    (S.SUBMITTED, S.REJECTED, "manager", "RECEIPT_REJECTED"),
    (S.REJECTED, S.REVIEW, "owner", "RECEIPT_REOPENED"),
]
ACTOR_KINDS = ["system", "owner", "other_employee", "manager"]

DISALLOWED_PAIRS = [
    (a, b) for a in S for b in S if (a, b) not in {(move[0], move[1]) for move in ALLOWED_MOVES}
]
WRONG_ACTOR_CASES = [(a, b, who, kind) for a, b, who, _ in ALLOWED_MOVES for kind in ACTOR_KINDS if kind != who]


@pytest.mark.parametrize("from_status, to_status, who, event_type", ALLOWED_MOVES, ids=lambda v: str(v))
def test_allowed_move_changes_status_and_writes_one_audit_event(
    session, owner, actors, from_status, to_status, who, event_type
):
    receipt = make_receipt(session, owner, status=from_status)
    actor = actors[who]

    transition(session, receipt, to_status, actor, {"note": "x"})

    assert receipt.status is to_status
    [event] = audit_events(session, receipt)
    assert (event.event_type, event.from_status, event.to_status) == (event_type, str(from_status), str(to_status))
    assert event.actor_id == (actor.id if actor else None)
    assert event.detail == {"note": "x"}


@pytest.mark.parametrize("from_status, to_status", DISALLOWED_PAIRS, ids=lambda v: str(v))
def test_every_other_pair_is_refused(session, owner, manager, from_status, to_status):
    receipt = make_receipt(session, owner, status=from_status)
    with pytest.raises(InvalidTransition):
        transition(session, receipt, to_status, manager)
    assert receipt.status is from_status
    assert audit_events(session, receipt) == []


def test_there_are_nine_allowed_moves_and_forty_refused_pairs():
    assert len(ALLOWED_MOVES) == 9
    assert len(DISALLOWED_PAIRS) == len(S) * len(S) - 9 == 40


@pytest.mark.parametrize("from_status, to_status, who, kind", WRONG_ACTOR_CASES, ids=lambda v: str(v))
def test_wrong_actor_is_refused(session, owner, actors, from_status, to_status, who, kind):
    receipt = make_receipt(session, owner, status=from_status)
    with pytest.raises(PermissionDenied):
        transition(session, receipt, to_status, actors[kind])
    assert receipt.status is from_status
    assert audit_events(session, receipt) == []


# The named functions, with the status each starts from and the kind of actor who may call it.
NAMED_FUNCTIONS = {
    "retry": (S.FAILED, "owner", lambda s, r, a: retry(s, r, a)),
    "choose_manual_entry": (S.FAILED, "owner", lambda s, r, a: choose_manual_entry(s, r, a)),
    "submit": (S.REVIEW, "owner", lambda s, r, a: submit(s, r, a)),
    "approve": (S.SUBMITTED, "manager", lambda s, r, a: approve(s, r, a)),
    "reject": (S.SUBMITTED, "manager", lambda s, r, a: reject(s, r, a, "Not acceptable")),
    "reopen": (S.REJECTED, "owner", lambda s, r, a: reopen(s, r, a)),
}
WRONG_STATUS_CASES = [(name, status) for name, (start, _, _) in NAMED_FUNCTIONS.items() for status in S if status != start]
WRONG_ACTOR_NAMED_CASES = [(name, kind) for name, (_, who, _) in NAMED_FUNCTIONS.items() for kind in ACTOR_KINDS if kind != who]


@pytest.mark.parametrize("name, status", WRONG_STATUS_CASES, ids=lambda v: str(v))
def test_named_function_refuses_the_wrong_status(session, owner, actors, name, status):
    _, who, function = NAMED_FUNCTIONS[name]
    receipt = make_receipt(session, owner, status=status)
    with pytest.raises(InvalidTransition):
        function(session, receipt, actors[who])
    assert receipt.status is status
    assert audit_events(session, receipt) == []


@pytest.mark.parametrize("name, kind", WRONG_ACTOR_NAMED_CASES, ids=lambda v: str(v))
def test_named_function_refuses_the_wrong_actor(session, owner, actors, name, kind):
    start, _, function = NAMED_FUNCTIONS[name]
    receipt = make_receipt(session, owner, status=start)
    with pytest.raises(PermissionDenied):
        function(session, receipt, actors[kind])
    assert receipt.status is start
    assert audit_events(session, receipt) == []


def test_workflow_functions_do_not_commit(session, owner, manager):
    receipt = make_receipt(session, owner, status=S.SUBMITTED)
    approve(session, receipt, manager)

    session.rollback()  # the caller never committed, so nothing should survive

    assert receipt.status is S.SUBMITTED
    assert session.scalars(select(Review)).all() == []
    assert audit_events(session, receipt) == []


# --- retry and manual entry ---------------------------------------------------


def test_retry_clears_the_failure_reason_and_records_who_retried(session, owner):
    receipt = make_receipt(session, owner, status=S.FAILED, failure_reason="Receipt was blurry")

    retry(session, receipt, owner)

    assert receipt.status is S.PROCESSING
    assert receipt.failure_reason is None
    [event] = audit_events(session, receipt)
    assert (event.event_type, event.from_status, event.to_status) == ("EXTRACTION_STARTED", "FAILED", "PROCESSING")
    assert event.actor_id == owner.id
    assert event.detail == {"extractor": settings.extractor}


def test_manual_entry_moves_a_failed_receipt_to_review(session, owner):
    receipt = make_receipt(session, owner, status=S.FAILED, failure_reason="Receipt was blurry")

    choose_manual_entry(session, receipt, owner)

    assert receipt.status is S.REVIEW
    [event] = audit_events(session, receipt)
    assert (event.event_type, event.actor_id, event.detail) == ("MANUAL_ENTRY_CHOSEN", owner.id, None)
    assert line_items(session, receipt) == []


# --- submit -------------------------------------------------------------------


def test_a_complete_receipt_can_be_submitted(session, owner):
    receipt = make_review_receipt(session, owner)

    submit(session, receipt, owner)

    assert receipt.status is S.SUBMITTED
    [event] = audit_events(session, receipt)
    assert (event.event_type, event.actor_id) == ("RECEIPT_SUBMITTED", owner.id)
    assert event.detail == {"totals_mismatch": None}


SUBMIT_FAILURES = [
    ("merchant missing", dict(merchant=None), "Merchant is required"),
    ("merchant blank", dict(merchant="   "), "Merchant is required"),
    ("date missing", dict(purchase_date=None), "Purchase date is required"),
    ("date in the future", dict(purchase_date=date(2999, 1, 1)), "cannot be in the future"),
    ("total missing", dict(total_cents=None), "Total is required"),
    ("total zero", dict(total_cents=0), "Total must be greater than zero"),
    ("total negative", dict(total_cents=-500), "Total must be greater than zero"),
    ("currency missing", dict(currency=None), "Currency is required"),
    ("tax negative", dict(tax_cents=-1), "Tax cannot be negative"),
    ("no line items", dict(items=[]), "At least one line item"),
    ("item without a category", dict(items=[(900, None)]), "choose a category"),
    ("item amount zero", dict(items=[(0, "meals")]), "amount must be greater than zero"),
    ("item amount negative", dict(items=[(-900, "meals")]), "amount must be greater than zero"),
]


@pytest.mark.parametrize("overrides, expected", [case[1:] for case in SUBMIT_FAILURES], ids=[case[0] for case in SUBMIT_FAILURES])
def test_incomplete_receipt_is_not_submitted(session, owner, overrides, expected):
    receipt = make_review_receipt(session, owner, **overrides)

    with pytest.raises(ValidationFailed) as error:
        submit(session, receipt, owner)

    assert any(expected in problem for problem in error.value.problems)
    assert receipt.status is S.REVIEW
    assert audit_events(session, receipt) == []


def test_every_problem_is_reported_at_once(session, owner):
    receipt = make_review_receipt(session, owner, items=[], merchant=None, total_cents=0)

    with pytest.raises(ValidationFailed) as error:
        submit(session, receipt, owner)

    assert len(error.value.problems) == 3


def test_a_missing_tax_is_fine(session, owner):
    receipt = make_review_receipt(session, owner, tax_cents=None, total_cents=900)
    submit(session, receipt, owner)
    assert receipt.status is S.SUBMITTED


def test_a_totals_mismatch_warns_but_does_not_block(session, owner):
    receipt = make_review_receipt(session, owner, total_cents=1500)  # items 900 + tax 100 = 1000

    submit(session, receipt, owner)

    assert receipt.status is S.SUBMITTED
    [event] = audit_events(session, receipt)
    assert event.detail == {
        "totals_mismatch": {"line_items_plus_tax_cents": 1000, "total_cents": 1500, "difference_cents": 500}
    }


def test_permission_is_checked_before_the_data(session, owner, other_employee):
    receipt = make_receipt(session, owner, status=S.REVIEW)  # incomplete on purpose
    with pytest.raises(PermissionDenied):
        submit(session, receipt, other_employee)


# --- approve, reject, reopen --------------------------------------------------


def test_approve_records_a_review_and_an_audit_event(session, owner, manager):
    receipt = make_receipt(session, owner, status=S.SUBMITTED)

    review = approve(session, receipt, manager, comment="  Looks good \n")

    assert receipt.status is S.APPROVED
    assert (review.decision.value, review.comment, review.manager_id) == ("approved", "Looks good", manager.id)
    [event] = audit_events(session, receipt)
    assert (event.event_type, event.actor_id, event.detail) == ("RECEIPT_APPROVED", manager.id, {"review_id": review.id})


@pytest.mark.parametrize("comment", [None, "", "   ", "\n"])
def test_approve_does_not_need_a_comment(session, owner, manager, comment):
    receipt = make_receipt(session, owner, status=S.SUBMITTED)
    review = approve(session, receipt, manager, comment=comment)
    assert review.comment is None


@pytest.mark.parametrize("comment", [None, "", "   ", "\t", "\n", " \t\n "])
def test_a_blank_rejection_comment_is_refused(session, owner, manager, comment):
    receipt = make_receipt(session, owner, status=S.SUBMITTED)

    with pytest.raises(ValidationFailed):
        reject(session, receipt, manager, comment)

    assert receipt.status is S.SUBMITTED
    assert session.scalars(select(Review)).all() == []
    assert audit_events(session, receipt) == []


def test_a_rejection_comment_is_saved_trimmed(session, owner, manager):
    receipt = make_receipt(session, owner, status=S.SUBMITTED)

    review = reject(session, receipt, manager, "  Missing itemized receipt\n")

    assert receipt.status is S.REJECTED
    assert review.comment == "Missing itemized receipt"
    [event] = audit_events(session, receipt)
    assert (event.event_type, event.detail) == ("RECEIPT_REJECTED", {"review_id": review.id})


@pytest.mark.parametrize("second", [approve, lambda s, r, a: reject(s, r, a, "Too late")])
def test_a_second_decision_is_refused(session, owner, manager, second):
    receipt = make_receipt(session, owner, status=S.SUBMITTED)
    approve(session, receipt, manager)
    session.commit()

    with pytest.raises(InvalidTransition):
        second(session, receipt, manager)

    assert receipt.status is S.APPROVED
    assert len(session.scalars(select(Review)).all()) == 1


def test_reopen_points_at_the_latest_rejection(session, owner, manager):
    receipt = make_review_receipt(session, owner)
    submit(session, receipt, owner)
    first = reject(session, receipt, manager, "First reason")
    reopen(session, receipt, owner)
    submit(session, receipt, owner)
    second = reject(session, receipt, manager, "Second reason")

    reopen(session, receipt, owner)

    assert receipt.status is S.REVIEW
    reopened = [e for e in audit_events(session, receipt) if e.event_type == "RECEIPT_REOPENED"]
    assert [e.detail for e in reopened] == [{"review_id": first.id}, {"review_id": second.id}]


# --- editing ------------------------------------------------------------------


def test_update_receipt_trims_text_and_standardizes_the_currency(session, owner):
    receipt = make_receipt(session, owner, status=S.REVIEW)

    update_receipt(
        session, receipt, owner,
        merchant="  Corner Cafe \n", description="\tTeam lunch\n", currency=" usd ",
        total_cents=1250, purchase_date=date(2026, 2, 1), tax_cents=0,
    )
    session.commit()

    assert receipt.merchant == "Corner Cafe"
    assert receipt.description == "Team lunch"
    assert receipt.currency == "USD"
    assert (receipt.total_cents, receipt.purchase_date, receipt.tax_cents) == (1250, date(2026, 2, 1), 0)
    assert audit_events(session, receipt) == []  # edits write no audit event


def test_a_blank_value_is_saved_as_null(session, owner):
    receipt = make_receipt(session, owner, status=S.REVIEW, merchant="Cafe", description="Lunch", currency="USD")

    update_receipt(session, receipt, owner, merchant="   ", description="", currency="\t")
    session.commit()

    assert (receipt.merchant, receipt.description, receipt.currency) == (None, None, None)


@pytest.mark.parametrize("bad_currency", ["US", "USDX", "12$", "u$d", "€€€"])
def test_a_bad_currency_is_refused_and_nothing_is_saved(session, owner, bad_currency):
    receipt = make_receipt(session, owner, status=S.REVIEW, merchant="Original")

    with pytest.raises(ValidationFailed):
        update_receipt(session, receipt, owner, merchant="Changed", currency=bad_currency)

    assert receipt.merchant == "Original"


@pytest.mark.parametrize("field", ["status", "owner_id", "extracted_data", "failure_reason"])
def test_only_the_edit_fields_can_be_changed(session, owner, field):
    receipt = make_receipt(session, owner, status=S.REVIEW)
    with pytest.raises(ValueError):
        update_receipt(session, receipt, owner, **{field: "x"})


@pytest.mark.parametrize("status", [s for s in S if s is not S.REVIEW])
def test_editing_is_only_allowed_in_review(session, owner, status):
    receipt = make_receipt(session, owner, status=status, merchant="Original")

    with pytest.raises(InvalidTransition):
        update_receipt(session, receipt, owner, merchant="Changed")
    with pytest.raises(InvalidTransition):
        set_line_items(session, receipt, owner, [(100, None)])

    assert receipt.merchant == "Original"


@pytest.mark.parametrize("kind", ["system", "other_employee", "manager"])
def test_only_the_owner_may_edit(session, owner, actors, kind):
    receipt = make_receipt(session, owner, status=S.REVIEW, merchant="Original")

    with pytest.raises(PermissionDenied):
        update_receipt(session, receipt, actors[kind], merchant="Changed")
    with pytest.raises(PermissionDenied):
        set_line_items(session, receipt, actors[kind], [(100, None)])

    assert receipt.merchant == "Original"


def test_set_line_items_replaces_all_of_them(session, owner):
    receipt = make_review_receipt(session, owner, items=[(300, "meals"), (400, None)])
    meals = meals_id(session)

    set_line_items(session, receipt, owner, [(500, meals), (600, None), (700, meals)])
    session.commit()

    assert [(i.amount_cents, i.category_id) for i in line_items(session, receipt)] == [(500, meals), (600, None), (700, meals)]
    assert audit_events(session, receipt) == []


# --- the background process ---------------------------------------------------


def test_process_receipt_applies_the_mock_result(session, owner, uploads, session_factory):
    receipt = make_uploaded_receipt(session, owner, uploads)
    mock = MockExtractor(delay_seconds=0, fail=False)

    process_receipt(receipt.id, extractor=mock, session_factory=session_factory)

    session.expire_all()
    names = list(session.scalars(select(Category.name).order_by(Category.id)))
    expected = mock.extract(uploads / "r1.bin", names)
    assert receipt.status is S.REVIEW
    assert receipt.failure_reason is None
    assert receipt.extracted_data == expected.model_dump(mode="json")
    assert receipt.merchant == expected.merchant
    assert receipt.purchase_date == expected.purchase_date
    assert receipt.total_cents == expected.total_cents
    assert receipt.currency == expected.currency
    assert receipt.tax_cents == expected.tax_cents
    assert receipt.confidence == expected.confidence
    items = line_items(session, receipt)
    assert [(i.amount_cents, i.category.name if i.category else None) for i in items] == [
        (i.amount_cents, i.category) for i in expected.line_items
    ]
    events = audit_events(session, receipt)
    assert [(e.event_type, e.from_status, e.to_status, e.actor_id) for e in events] == [
        ("EXTRACTION_STARTED", "UPLOADED", "PROCESSING", None),
        ("EXTRACTION_SUCCEEDED", "PROCESSING", "REVIEW", None),
    ]
    assert events[0].detail == {"extractor": settings.extractor}
    assert events[1].detail == {"extractor": settings.extractor, "line_item_count": len(expected.line_items)}


def test_an_unknown_category_leaves_the_category_empty(session, owner, uploads, session_factory):
    receipt = make_uploaded_receipt(session, owner, uploads)
    result = ExtractionResult(
        merchant="  Corner Cafe  ",
        purchase_date=date(2026, 1, 15),
        total_cents=1500,
        currency="USD",
        line_items=[
            ExtractedLineItem(amount_cents=500, category="Meals"),
            ExtractedLineItem(amount_cents=500, category="Gadgets"),  # not in the company list
            ExtractedLineItem(amount_cents=500, category=None),
        ],
    )

    process_receipt(receipt.id, extractor=FixedExtractor(result), session_factory=session_factory)

    session.expire_all()
    assert [i.category_id for i in line_items(session, receipt)] == [meals_id(session), None, None]
    assert receipt.merchant == "Corner Cafe"  # trimmed in the column
    assert receipt.extracted_data["merchant"] == "  Corner Cafe  "  # raw output untouched


def test_an_extraction_error_message_is_stored_for_the_employee(session, owner, uploads, session_factory):
    receipt = make_uploaded_receipt(session, owner, uploads)

    process_receipt(receipt.id, extractor=RaisingExtractor(ExtractionError("The photo is too blurry")), session_factory=session_factory)

    session.expire_all()
    assert receipt.status is S.FAILED
    assert receipt.failure_reason == "The photo is too blurry"
    assert receipt.extracted_data is None
    assert line_items(session, receipt) == []
    started, failed = audit_events(session, receipt)
    assert (failed.event_type, failed.from_status, failed.to_status, failed.actor_id) == (
        "EXTRACTION_FAILED", "PROCESSING", "FAILED", None
    )
    assert failed.detail == {"reason": "The photo is too blurry", "kind": "extraction_error"}


def test_the_mock_failure_flag_ends_in_failed(session, owner, uploads, session_factory):
    receipt = make_uploaded_receipt(session, owner, uploads)

    process_receipt(receipt.id, extractor=MockExtractor(delay_seconds=0, fail=True), session_factory=session_factory)

    session.expire_all()
    assert receipt.status is S.FAILED
    assert receipt.failure_reason


def test_an_unexpected_error_gets_a_generic_reason_and_is_logged(session, owner, uploads, session_factory, caplog):
    receipt = make_uploaded_receipt(session, owner, uploads)

    with caplog.at_level(logging.ERROR, logger="app.services.workflow"):
        process_receipt(
            receipt.id, extractor=RaisingExtractor(RuntimeError("secret internal detail")), session_factory=session_factory
        )

    session.expire_all()
    assert receipt.status is S.FAILED
    assert receipt.failure_reason == GENERIC_FAILURE_REASON
    failed = audit_events(session, receipt)[-1]
    assert failed.actor_id is None
    assert failed.detail == {"reason": GENERIC_FAILURE_REASON, "kind": "unexpected"}
    assert "secret internal detail" not in str(failed.detail)
    assert "secret internal detail" in caplog.text  # the real error is in the logs


def test_the_ollama_setting_ends_in_failed_not_stuck_in_processing(session, owner, uploads, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "extractor", "ollama")  # get_extractor() raises NotImplementedError
    receipt = make_uploaded_receipt(session, owner, uploads)

    process_receipt(receipt.id, session_factory=session_factory)

    session.expire_all()
    assert receipt.status is S.FAILED
    assert receipt.failure_reason == GENERIC_FAILURE_REASON


def test_a_failure_after_rows_were_added_leaves_no_partial_data(session, owner, uploads, session_factory, monkeypatch):
    def add_an_item_then_fail(db_session, receipt, result):
        db_session.add(LineItem(receipt_id=receipt.id, amount_cents=100))
        db_session.flush()
        raise RuntimeError("database trouble")

    monkeypatch.setattr(workflow, "_apply_extraction_result", add_an_item_then_fail)
    receipt = make_uploaded_receipt(session, owner, uploads)

    process_receipt(receipt.id, extractor=MockExtractor(delay_seconds=0, fail=False), session_factory=session_factory)

    session.expire_all()
    assert receipt.status is S.FAILED
    assert line_items(session, receipt) == []


def test_retry_after_a_failure_reaches_review(session, owner, uploads, session_factory):
    receipt = make_uploaded_receipt(session, owner, uploads)
    process_receipt(receipt.id, extractor=MockExtractor(delay_seconds=0, fail=True), session_factory=session_factory)
    session.expire_all()
    assert receipt.status is S.FAILED

    retry(session, receipt, owner)
    session.commit()
    process_receipt(receipt.id, extractor=MockExtractor(delay_seconds=0, fail=False), session_factory=session_factory)

    session.expire_all()
    assert receipt.status is S.REVIEW
    assert receipt.failure_reason is None
    assert [(e.event_type, e.actor_id) for e in audit_events(session, receipt)] == [
        ("EXTRACTION_STARTED", None),
        ("EXTRACTION_FAILED", None),
        ("EXTRACTION_STARTED", owner.id),
        ("EXTRACTION_SUCCEEDED", None),
    ]


@pytest.mark.parametrize("status", [S.REVIEW, S.SUBMITTED, S.APPROVED, S.REJECTED, S.FAILED])
def test_process_receipt_leaves_other_statuses_alone(session, owner, uploads, session_factory, status):
    receipt = make_uploaded_receipt(session, owner, uploads, status=status)

    # If the extractor were called, this would end in FAILED.
    process_receipt(receipt.id, extractor=RaisingExtractor(RuntimeError("must not run")), session_factory=session_factory)

    session.expire_all()
    assert receipt.status is status
    assert audit_events(session, receipt) == []


def test_process_receipt_ignores_a_missing_receipt(session_factory):
    process_receipt(9999, session_factory=session_factory)  # logs an error, does not raise


# --- the whole flow -----------------------------------------------------------


def test_upload_to_approval_with_one_rejection(session, owner, manager, uploads, session_factory):
    receipt = make_uploaded_receipt(session, owner, uploads)
    process_receipt(receipt.id, extractor=MockExtractor(delay_seconds=0, fail=False), session_factory=session_factory)
    session.expire_all()
    assert receipt.status is S.REVIEW

    # The employee reviews: fixes the date and picks a category for every line item.
    update_receipt(session, receipt, owner, purchase_date=date(2026, 1, 15))
    meals = meals_id(session)
    set_line_items(session, receipt, owner, [(item.amount_cents, meals) for item in line_items(session, receipt)])
    submit(session, receipt, owner)
    session.commit()
    assert receipt.status is S.SUBMITTED

    reject(session, receipt, manager, "Please attach the itemized receipt")
    session.commit()
    reopen(session, receipt, owner)
    submit(session, receipt, owner)
    approve(session, receipt, manager)
    session.commit()

    assert receipt.status is S.APPROVED
    assert [e.event_type for e in audit_events(session, receipt)] == [
        "EXTRACTION_STARTED",
        "EXTRACTION_SUCCEEDED",
        "RECEIPT_SUBMITTED",
        "RECEIPT_REJECTED",
        "RECEIPT_REOPENED",
        "RECEIPT_SUBMITTED",
        "RECEIPT_APPROVED",
    ]
    assert len(session.scalars(select(Review)).all()) == 2
