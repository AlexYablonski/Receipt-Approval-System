# Receipt Approval System

A lightweight web app where employees upload receipts, AI extracts the data, the employee reviews and submits, and a manager approves or rejects.

> Work in progress. So far: the project scaffolding, the database schema (tables and migrations), the receipt extractor interface with a mock extractor, and the status workflow.

## Requirements

- Docker with the Compose plugin, **or** Python 3.13
- **Internet access in the browser.** The UI loads HTMX from a CDN (unpkg.com). Pages render offline, but HTMX interactions won't work. The pinned version is set in `app/templates/base.html`.

## Run with Docker

```bash
docker compose up --build
```

Open http://localhost:8000. Health check: http://localhost:8000/health

The database is created and brought up to date automatically each time the container starts (`alembic upgrade head`). It is stored in `./data/app.db` on your machine, so it survives restarts.

## Run without Docker

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
alembic upgrade head             # creates and migrates data/app.db; run from the repo root
uvicorn app.main:app --reload
```

## Configuration

Settings come from environment variables. For local use, copy `.env.example` to `.env` (optional; the defaults work).

| Variable | Values | Default |
| --- | --- | --- |
| `EXTRACTOR` | `mock`, `ollama` | `mock` |
| `DATABASE_URL` | a SQLAlchemy database URL (SQLite) | `sqlite:///data/app.db` |
| `MOCK_DELAY_SECONDS` | seconds the mock extractor waits, 0 or more (only used when `EXTRACTOR=mock`) | `2` |
| `MOCK_FAIL` | `true` makes every mock extraction fail (only used when `EXTRACTOR=mock`) | `false` |
| `UPLOAD_DIR` | folder where uploaded receipt files are stored | `data/uploads` |

An invalid `EXTRACTOR` value or a negative `MOCK_DELAY_SECONDS` stops the app at startup with an error. A relative SQLite path in `DATABASE_URL`, and a relative `UPLOAD_DIR`, are relative to where the app starts (the repo root without Docker, `/app` in Docker).

## Database

SQLite, accessed with SQLAlchemy 2.0. The schema is managed with Alembic migrations (`alembic/versions/`) and applied with `alembic upgrade head`. Docker runs this on every container start; without Docker, run it yourself (see above).

Tables: `users`, `categories` (the company's cost categories; the first migration inserts a placeholder starting list: Meals, Travel, Lodging, Office supplies, Other), `receipts`, `line_items`, `reviews` (one row per manager decision, so earlier rejection comments are never overwritten) and `audit_events`.

Design notes and limitations:

- **`FAILED` status.** It is an additional status beyond Uploaded, Processing, Review, Submitted, Approved and Rejected. A receipt whose extraction fails moves to `FAILED`, keeps its uploaded file and can be retried, instead of being stuck in Processing.
- **Money is stored as integer cents.** This assumes currencies with two decimal places. Currencies with none (JPY) or three (KWD) are not handled correctly.

## Receipt extraction

Extractors read an uploaded file and return the receipt's data. They all follow one interface (`app/services/extractors/base.py`): `extract(file_path, categories)`, where `categories` is the company's list of category names. The result holds the merchant, purchase date, total, currency, tax, the line items (an amount and a guessed category each) and a confidence score per field. An extractor never touches the database, and raises `ExtractionError` if it cannot read the receipt. The `EXTRACTOR` setting picks which one runs (`get_extractor()` in `app/services/extractors/__init__.py`).

- **Mock (default).** It does not read the receipt. It invents a plausible one from the file's SHA-256 hash, so the same file always gives the same result, and it waits `MOCK_DELAY_SECONDS` to imitate a slow AI call. Setting `MOCK_FAIL=true` makes every extraction fail, to test the failed state and retry.
- **Ollama.** Planned, for live extraction with a local model. The `EXTRACTOR` setting accepts `ollama`, but the extractor is not built yet.

Design notes and limitations:

- **Totals.** The extractor contract assumes every receipt has at least one line item and that the line items plus tax equal the total. The mock always satisfies this. Real receipts can break it (see Future improvements).
- **Confidence scores** are a dictionary of scores from 0 to 1 (one per field, plus one entry per line item), meant to be stored as-is in the `receipts.confidence` JSON column.

## Status workflow

All status changes go through `app/services/workflow.py`, which checks that the move is allowed, checks that the person making it is allowed to, and writes an audit event. Nothing else sets `receipts.status`.

| Move | Made by | Function |
| --- | --- | --- |
| Uploaded -> Processing | the system (background task) | `process_receipt` |
| Processing -> Review | the system | `process_receipt` |
| Processing -> Failed | the system | `process_receipt` |
| Failed -> Processing (retry) | the receipt's owner | `retry` |
| Failed -> Review (enter the details by hand) | the owner | `choose_manual_entry` |
| Review -> Submitted | the owner | `submit` |
| Submitted -> Approved | a manager | `approve` |
| Submitted -> Rejected | a manager | `reject` |
| Rejected -> Review (edit and resubmit) | the owner | `reopen` |

Any other move is refused. Fields and line items can only be edited in Review, by the owner (`update_receipt`, `set_line_items`).

Design notes and limitations:

- **Submit rules.** A receipt can be submitted when it has a merchant, a purchase date (not in the future), a total above zero and a currency; at least one line item; an amount above zero and a category on every line item; and a tax that is not negative, if present. Every problem is reported at once.
- **Totals mismatch.** If the line items plus tax differ from the total, the receipt can still be submitted. The difference is recorded in the audit event so the manager can see it (see Future improvements).
- **Text fields.** Spaces, tabs and newlines at the ends of text are trimmed before saving, and a blank value counts as missing. The currency is stored in capitals and must be three letters. A rejection needs a comment. The database check on the rejection comment is only a backstop: it catches empty and spaces-only comments, and the workflow catches the rest.
- **Errors.** The workflow raises `InvalidTransition`, `PermissionDenied` or `ValidationFailed` (all subclasses of `WorkflowError`) before it writes anything, so a refused action changes nothing.
- **Commits.** Workflow functions add rows but do not commit. The caller commits once, so a status change, its audit event and any review row succeed or fail together. `process_receipt` runs after the response has been sent, as a background task, so it opens its own database session and commits itself.
- **Failed extraction.** If the extractor raises `ExtractionError`, its message becomes the receipt's `failure_reason`. Any other error stores a generic reason and logs the details, so a receipt is not left in Processing.

## Tests

```bash
pytest
```

## Troubleshooting

- **Docker on Linux, permission errors on `./data`:** the container runs as uid 1000 (a non-root user). This was tested on Docker Desktop for Windows, where it is handled for you. On Linux it works out of the box when your user's uid is 1000 (typically the first user account); otherwise change the owner of `data/` or set `user:` in `docker-compose.yml`. A wrongly owned `data/` usually shows up as a SQLite `unable to open database file` error from the migration step when the container starts.

## AI tools used

_To be completed. Development log: `docs/AI_LOG.md`._

## Future improvements

- **Field-level edit history.** Today an employee's edits overwrite the receipt's columns, the raw AI output is kept unchanged in `receipts.extracted_data`, and `audit_events` records status changes only. So the net difference between the AI's values and the final values is visible, but not who changed what, when, or any intermediate edits. A field-level history would close that gap, which matters for audits and disputes. Either write a `FIELD_EDITED` event into `audit_events.detail` (`{field, old, new}`) whenever an edit is saved, or add a `receipt_edits` table (receipt_id, field, old, new, actor_id, created_at). It needs one new migration and one write at the edit endpoint. The saved `extracted_data` is the baseline the first edit is compared against.
- **Link a duplicate to its original (`duplicate_of_id`).** Today, uploads are checked against existing receipts with the same `file_hash` and flagged. The flag is worked out when a receipt is displayed, and no specific link is stored. A nullable, self-referencing `receipts.duplicate_of_id`, set at upload in a second migration, would let a manager click through to the exact earlier receipt, and would make the flag a stable record that can be queried and reported on. This is one migration (a column plus a foreign key, rebuilt in batch mode on SQLite) and a small change to the upload code.
- **Employee and manager departures.** Today, foreign keys block deleting a user who still has receipts, reviews or audit events. A standard archive process for separations could move the departing user's rows (and files) into archive tables, possibly with an `is_active` flag on `users` to deactivate login first. Open point: how to archive a manager whose decisions are referenced by other people's receipts.
- **Manager-defined categories.** Today there is one company-wide `categories` list, defined by the company. Letting managers add or edit categories for their own team or department would give flexibility in a large company. Structurally this means a nullable owner or department column on `categories`, name uniqueness per scope instead of global (a constraint change, so a batch-mode migration), and a manager-facing screen with a permission check.
- **Retiring categories.** Today the list can grow (new categories are added by migration), but a category that line items use cannot be deleted (foreign key, no cascading deletes), so retiring one isn't supported. The same kind of archive process as for users could move retired categories to an archive table while old line items keep their reference. A lighter first step is an `is_active` flag on `categories` (one migration, plus a filter so the category dropdown hides inactive ones).
- **Rounding, tips and discounts in the totals.** Today the extractor contract assumes the line items plus tax equal the total, and the mock always produces receipts that do. Real receipts often break this: tax may be rounded per item or per receipt, a tip or service charge may be added after tax, and discounts or coupons reduce the total. Handling them could mean allowing a tolerance of a few cents, extracting tips and discounts as their own values, and showing a mismatch to the employee as a warning with the reason instead of a hard error. The status workflow deliberately takes the softer line for now: a mismatch does not block submission. It is recorded in the submit audit event so the manager can see it, because blocking would strand legitimate receipts that include a tip, a discount or per-item tax rounding. Once those cases are handled, the goal is to turn this warning into a hard rule.
- **Refunds, credit notes and discount lines.** Today a receipt can only be submitted with a positive total, and every line item must have a positive amount, so a refund (a negative total) or a discount line (a negative line item) cannot be submitted. Supporting them touches several places: the extractor has to read the sign correctly (receipts often print a refund without a minus sign), the form has to accept a minus sign, the UI has to make a refund obvious to the manager, and the totals rule has to hold with negative amounts. The submit rules also reject a purchase date in the future, which catches typing mistakes and placeholder dates.
- **Time zones in the future-date check.** Today a receipt is refused at submit if its purchase date is after today's date in UTC. For someone whose local date is ahead of UTC (in Australia, early in the morning, for example), a valid same-day receipt is refused for a few hours each day. Possible fixes: allow one day of tolerance, or compare against today's date in a company time zone set by an environment variable.
- **Stale-state sweeper.** Today nothing moves a receipt that stops making progress. The motivating case: if the server restarts while a receipt is being extracted, the background thread dies with it and the receipt stays in `PROCESSING` forever, because nothing else will move it. A scheduled sweep would find receipts that have sat in one status longer than a threshold and handle each kind:
  - `PROCESSING` older than N minutes: move to `FAILED` with a reason such as "interrupted", so the employee can retry or enter the data by hand.
  - `UPLOADED` but never processed: start extraction again, or move to `FAILED` if it cannot start.
  - `FAILED` and not reviewed: remind the employee to retry or enter the data by hand.
  - `REVIEW` left untouched: remind the employee to finish and submit.
  - `SUBMITTED` waiting on a manager: remind or escalate to the manager.
  - `REJECTED` older than a threshold: close it or archive it, or remind the employee.

  Thresholds would be environment variables, and every action the sweeper takes would be written as an audit event with no actor (the system).
- **Manager assignment and org charts.** Today the system assumes one manager and one employee. Any manager can decide any submitted receipt, and nothing stops a manager from approving their own receipt. A real company needs each receipt to go to one specific manager. A first step is a nullable self-referencing `users.manager_id` (each employee has one manager), plus a `receipts.assigned_manager_id` copied when the receipt is submitted, so the receipt keeps a permanent record of who it was sent to even if the org chart changes later. That gives the manager a simple queue (their assigned, submitted receipts) and lets the workflow check that only that manager may decide, and that nobody decides their own receipt. Companies with several approvers per employee would need a many-to-many table and a way to choose one at submit time. This needs one migration (a column and a foreign key, rebuilt in batch mode on SQLite), seeded demo relationships, and changes to the manager screen and the workflow checks.
- **Simultaneous decisions on one receipt.** Before saving a manager's decision, the workflow checks that the receipt is still `SUBMITTED`. That check and the write are two separate steps, so if two managers decide the same receipt at the same instant, both could pass the check, and the later write would win. With one manager in the assumed user network this is unlikely, and I accepted it for simplicity. The fix is a conditional update (`UPDATE receipts SET status = ... WHERE id = ? AND status = 'SUBMITTED'`) that checks how many rows changed, together with the manager assignment above so only one manager can decide a given receipt.
