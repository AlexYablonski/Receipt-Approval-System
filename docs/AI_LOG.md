# AI Log

One entry per module: which tool was used, what it did, and what I changed by hand.

## Module 1: Scaffolding & Docker (2026-11-20)

- **Tool:** Claude (Sonnet 5.5) in a claude.ai Project chat.
- **What it did:** Proposed the design choices (config loading, test client, HTMX delivery, Docker run mode); drafted the app skeleton, `/health` endpoint, base template, Dockerfile, compose file, requirements, housekeeping files and smoke tests; checked that the pinned dependencies install and the tests pass on Python 3.13.
- **What I changed by hand:** ran docker compose commands and verified output. built app. checked connection to port. checked app status in cmd prompt. installed dependcies from requirements.txt. ran pytest which returned 5 passed tests, 1 warning on HTMX version as expected. Made 3 by hand edits to app/config.py, .env.example, and README.md to reflect no Claude extractor option. Ran the commit to public github repo.
