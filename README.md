# Irmscher Parts Tracker

A personal Opel Signum Irmscher parts tracker. It discovers listings from the
eBay Browse API and public SS.com RSS feeds, stores current state and history in
SQLite, applies deterministic watchlist matching, and can send Telegram alerts.

The supported deployment is one Docker container, one Uvicorn process, and a
persistent `data/` directory. The tracker does not purchase items, contact
sellers, use marketplace logins, or collect seller contact details.

## Requirements

- Docker Desktop or Docker Engine with Compose
- eBay Browse API credentials only when eBay is enabled
- Optional Telegram bot token and chat ID

## First start

Copy `.env.example` to `.env`, choose the enabled sources, then run:

```bash
./tracker.sh init
```

```powershell
.\tracker.ps1 init
```

For credential-free SS.com operation, set:

```dotenv
TRACKER_EBAY_ENABLED=false
TRACKER_SSCOM_ENABLED=true
```

`init` builds the image, creates `data/`, generates a random API token only
when the token is blank, migrates the database, starts the service, and waits
for health. Existing tokens and databases are preserved.

Open <http://localhost:8000/docs> for the generated API documentation.

## Configuration

All environment variables use the `TRACKER_` prefix.

| Variable | Default | Purpose |
|---|---:|---|
| `TRACKER_API_TOKEN` | generated | Bearer token for scan requests |
| `TRACKER_DATABASE_URL` | `/app/data/tracker.db` | SQLite database URL |
| `TRACKER_EBAY_ENABLED` | `true` | Enable eBay |
| `TRACKER_EBAY_CLIENT_ID` | empty | eBay Browse API client ID |
| `TRACKER_EBAY_CLIENT_SECRET` | empty | eBay Browse API secret |
| `TRACKER_SEARCH_INTERVAL_MINUTES` | `30` | eBay schedule interval |
| `TRACKER_SSCOM_ENABLED` | `false` | Enable SS.com |
| `TRACKER_SSCOM_INTERVAL_MINUTES` | `60` | SS.com schedule interval |
| `TRACKER_SSCOM_REQUEST_TIMEOUT` | `20` | Request timeout in seconds |
| `TRACKER_SSCOM_MAX_DETAIL_REQUESTS_PER_RUN` | `30` | Detail-page budget |
| `TRACKER_SSCOM_DETAIL_REFRESH_HOURS` | `24` | Cached-detail refresh age |
| `TRACKER_SCAN_ON_STARTUP` | `true` | Scan each ready source at startup |
| `TRACKER_MAX_CONSECUTIVE_MISSES` | `3` | Complete discoveries before deactivation |
| `TRACKER_TELEGRAM_ENABLED` | `true` | Enable Telegram delivery |

The SS.com feed list is in `config/sources.yaml`, mounted read-only in the
container. The defaults cover Signum and Vectra parts and donor cars. Feed URLs
must use HTTPS on `www.ss.com` and point to a supported RSS category.

SS.com prices may be absent. Such listings are stored with a null price and do
not generate price-drop alerts.

## Operations

The Bash and PowerShell scripts support equivalent commands:

```text
init
start
stop
restart
status
logs
scan-source ebay
scan-source sscom
backup [filename]
restore <filename>
doctor
update
```

Examples:

```bash
./tracker.sh scan-source sscom
./tracker.sh backup
./tracker.sh restore backup_20260801_120000.sqlite
```

`scan-source` submits a background run and polls it to a terminal state. Direct
CLI use can return immediately or wait:

```bash
tracker trigger-scan sscom
tracker trigger-scan sscom --wait
```

Restore accepts only a filename directly under `data/`, creates a timestamped
pre-restore backup, stops the service, restores with SQLite's backup API, and
restarts only after success.

## API

- `GET /health` reports database, scheduler, eBay, SS.com, and Telegram state.
- `GET /listings` and `GET /listings/{id}` expose stored listings.
- `GET /matches` exposes current part-specific matches.
- `GET /search-runs` lists scan history.
- `GET /search-runs/{id}` returns one run for polling.
- `POST /runs/{source}` requires `Authorization: Bearer <token>` and returns
  HTTP 202 with a running search-run ID.

Manual and scheduled scans share one lock per source. An overlap returns HTTP
409 with the active run ID. Feed discovery completeness controls misses;
detail-enrichment failures make a run partial without hiding listings observed
in complete feeds.

## SS.com behavior and limits

The adapter uses public RSS and ordinary public listing pages only. It validates
every redirect, restricts fetches to `www.ss.com`, limits response sizes,
retries only temporary failures, honors `Retry-After`, and spaces requests
conservatively. It does not access inbox, login, deletion, abuse-reporting,
ad-management, phone-reveal, or email endpoints.

Parsing uses category labels and semantic identifiers from server-rendered
HTML. A layout change can make enrichment partial while RSS discovery continues.
Update the sanitized fixtures and parser tests before changing selectors.

## Local development

```bash
uv sync
uv run tracker db upgrade
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
docker compose config
```

## Troubleshooting

- Container exits: inspect `docker compose logs`; migrations or required core
  settings failed before Uvicorn started.
- `/health` returns 503: the database failed or a scheduler task stopped.
- Scan returns 400: the source is disabled, unconfigured, or unsupported.
- Scan returns 409: that source already has an active run.
- SS.com run is partial: inspect `/search-runs/{id}` and logs for a failed feed,
  detail page, content-type rejection, redirect rejection, or exhausted detail
  budget.
- No Telegram alerts: verify both Telegram values and run
  `docker compose exec irmscher-tracker tracker test-notification`.

Kleinanzeigen, Allegro, Ovoko, visual recognition, CAPTCHA handling, proxies,
authentication automation, purchasing, and seller contact are out of scope.
