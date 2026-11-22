# Receipt Approval System

A lightweight web app where employees upload receipts, AI extracts the data, the employee reviews and submits, and a manager approves or rejects.

> Work in progress. So far: the project scaffolding and the database schema (tables and migrations).

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

An invalid `EXTRACTOR` value stops the app at startup with an error. A relative SQLite path in `DATABASE_URL` is relative to where the app starts (the repo root without Docker, `/app` in Docker).

## Database

SQLite, accessed with SQLAlchemy 2.0. The schema is managed with Alembic migrations (`alembic/versions/`) and applied with `alembic upgrade head`. Docker runs this on every container start; without Docker, run it yourself (see above).

Tables: `users`, `categories` (the company's cost categories; the first migration inserts a placeholder starting list: Meals, Travel, Lodging, Office supplies, Other), `receipts`, `line_items`, `reviews` (one row per manager decision, so earlier rejection comments are never overwritten) and `audit_events`.

Design notes and limitations:

- **`FAILED` status.** It is an additional status beyond Uploaded, Processing, Review, Submitted, Approved and Rejected. A receipt whose extraction fails moves to `FAILED`, keeps its uploaded file and can be retried, instead of being stuck in Processing.
- **Money is stored as integer cents.** This assumes currencies with two decimal places. Currencies with none (JPY) or three (KWD) are not handled correctly.

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
