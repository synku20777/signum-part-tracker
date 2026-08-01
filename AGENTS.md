# AGENTS.md

## Project purpose

This repository implements a personal Opel Signum Irmscher parts tracker.

It searches approved marketplace APIs and permitted public search pages, stores
listing history, scores listings against an explicit parts watchlist and sends
alerts for new matches and meaningful price changes.

It must not purchase items, contact sellers automatically, bypass access
controls, solve CAPTCHAs or evade marketplace rate limits.

## Technical standards

- Use Python 3.12 or newer.
- Use uv for dependency and virtual-environment management.
- Use src layout.
- Use type hints throughout.
- Use Pydantic models at all external-data boundaries.
- Use SQLAlchemy 2.x patterns.
- Use Alembic for schema migrations.
- Use async HTTP with httpx where practical.
- Use Playwright only when an API or ordinary HTTP parser is insufficient.
- Keep marketplace-specific parsing inside source adapters.
- Never expose raw marketplace response structures to application services.
- Store all timestamps in UTC.
- Use Decimal for prices.
- Never commit credentials, tokens, cookies or browser session data.

## Quality gates

Before completing a task, run:

- uv run ruff check .
- uv run ruff format --check .
- uv run mypy src
- uv run pytest

Fix failures rather than suppressing them unless suppression is explicitly
justified.

## Scraper rules

- Respect source terms, access restrictions and reasonable request intervals.
- Do not bypass CAPTCHAs or anti-bot systems.
- Use stable identifiers and structured data before CSS selectors.
- Store sanitized HTML fixtures for parser tests.
- Add parser tests before changing selectors after a source layout change.
- Every adapter must implement timeouts, retries with backoff and structured
  error logging.
- One failed source must not stop other sources from running.
- Browser contexts must be closed in finally blocks.
- Do not log authentication tokens, cookies or personal data.

## Matching rules

- Exact normalized part-number matches have highest priority.
- Matching must remain deterministic and explainable.
- Store the reasons contributing to every score.
- Negative part numbers and incompatible models must override fuzzy matches.
- Do not use an LLM for first-stage listing classification.

## Delivery rules

- Make small, reviewable changes.
- Explain important design decisions.
- Add or update tests with every behavior change.
- Update README setup instructions whenever configuration changes.