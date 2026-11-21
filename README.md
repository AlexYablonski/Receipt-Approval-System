# Receipt Approval System

A lightweight web app where employees upload receipts, AI extracts the data, the employee reviews and submits, and a manager approves or rejects.

> Work in progress. Only the project scaffolding exists so far.

## Requirements

- Docker with the Compose plugin, **or** Python 3.13
- **Internet access in the browser.** The UI loads HTMX from a CDN (unpkg.com). Pages render offline, but HTMX interactions won't work. The pinned version is set in `app/templates/base.html`.

## Run with Docker

```bash
docker compose up --build
```

Open http://localhost:8000. Health check: http://localhost:8000/health

## Run without Docker

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Configuration

Settings come from environment variables. For local use, copy `.env.example` to `.env` (optional; the defaults work).

| Variable | Values | Default |
| --- | --- | --- |
| `EXTRACTOR` | `mock`, `ollama` | `mock` |

An invalid value stops the app at startup with an error.

## Tests

```bash
pytest
```

## Troubleshooting

- **Docker on Linux, permission errors on `./data`:** the container runs as uid 1000. If your user has a different uid, change the owner of `data/` or set `user:` in `docker-compose.yml`.

## AI tools used

_To be completed. Development log: `docs/AI_LOG.md`._

## Future improvements

_To be completed._
