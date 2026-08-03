# Irmscher Parts Tracker

A personal Opel Signum Irmscher parts tracker. It discovers listings from the
eBay Browse API and public SS.com RSS feeds, stores current state and history in
SQLite, applies deterministic watchlist matching, and can send Telegram alerts.
It also includes a private manual-review page for building a sanitized,
human-approved reference-image gallery.

The supported deployment is one Docker container, one Uvicorn process, and a
persistent `data/` directory. The tracker does not purchase items, contact
sellers, use marketplace logins, or collect seller contact details.

## Requirements

- Docker Desktop or Docker Engine with Compose
- eBay application credentials and deletion-compliance callback when Production eBay is enabled
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

Open <http://localhost:8000/docs> for API documentation or
<http://localhost:8000/review> for the manual-review page.

## Configuration

All environment variables use the `TRACKER_` prefix.

| Variable | Default | Purpose |
|---|---:|---|
| `TRACKER_API_TOKEN` | generated | Bearer token for scan requests |
| `TRACKER_DATABASE_URL` | `/app/data/tracker.db` | SQLite database URL |
| `TRACKER_EBAY_ENABLED` | `false` | Enable eBay |
| `TRACKER_EBAY_ENVIRONMENT` | `production` | `sandbox` or `production` API endpoints |
| `TRACKER_EBAY_CLIENT_ID` | empty | eBay Browse API client ID |
| `TRACKER_EBAY_CLIENT_SECRET` | empty | eBay Browse API secret |
| `TRACKER_EBAY_DELETION_ENDPOINT_URL` | empty | Exact public deletion callback URL |
| `TRACKER_EBAY_DELETION_VERIFICATION_TOKEN` | empty | Secret 32–80 character callback token |
| `TRACKER_EBAY_DELETION_MAX_PENDING_HOURS` | `24` | Maximum healthy deletion-processing age |
| `TRACKER_SEARCH_INTERVAL_MINUTES` | `30` | eBay schedule interval |
| `TRACKER_SSCOM_ENABLED` | `false` | Enable SS.com |
| `TRACKER_SSCOM_INTERVAL_MINUTES` | `60` | SS.com schedule interval |
| `TRACKER_SSCOM_REQUEST_TIMEOUT` | `20` | Request timeout in seconds |
| `TRACKER_SSCOM_MAX_DETAIL_REQUESTS_PER_RUN` | `30` | Detail-page budget |
| `TRACKER_SSCOM_DETAIL_REFRESH_HOURS` | `24` | Cached-detail refresh age |
| `TRACKER_SCAN_ON_STARTUP` | `true` | Scan each ready source at startup |
| `TRACKER_MAX_CONSECUTIVE_MISSES` | `3` | Complete discoveries before deactivation |
| `TRACKER_TELEGRAM_ENABLED` | `true` | Enable Telegram delivery |
| `TRACKER_REVIEW_CAMPAIGN_TARGET` | `100` | Advisory distinct-listing review target |
| `TRACKER_REVIEW_CONFIRMED_LISTINGS_TARGET` | `3` | Confirmed listings wanted per part |
| `TRACKER_REVIEW_POSITIVE_REFERENCES_TARGET` | `5` | Active positive images wanted per part |
| `TRACKER_REVIEW_NEGATIVE_LISTINGS_TARGET` | `5` | Distinct negative listings wanted per part |
| `TRACKER_REVIEW_NEGATIVE_REFERENCES_TARGET` | `10` | Active negative images wanted per part |

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
./tracker.sh restore backup_20260801_120000.tar.gz
```

`scan-source` submits a background run and polls it to a terminal state. Direct
CLI use can return immediately or wait:

```bash
tracker trigger-scan sscom
tracker trigger-scan sscom --wait
tracker review doctor
tracker review doctor --repair
tracker review export /app/data/exports/pilot-dataset
```

The default backup is one `.tar.gz` containing a consistent SQLite copy, the
sanitized `references/` gallery, and a hash-verified manifest. An explicitly
named `.sqlite` file remains supported for database-only legacy backups; it is
incomplete once reference images exist. Restore accepts either format directly
under `data/`, creates a full pre-restore archive, stops the service, validates
and restores in a one-shot container, and recovers the pre-restore archive if
the requested restore fails. A legacy SQLite restore preserves current
reference files.

## API

- `GET /health` reports database, scheduler, eBay, SS.com, and Telegram state.
- `GET /listings` and `GET /listings/{id}` expose stored listings.
- `GET /matches` exposes current part-specific matches.
- `GET /search-runs` lists scan history.
- `GET /search-runs/{id}` returns one run for polling.
- `POST /runs/{source}` requires `Authorization: Bearer <token>` and returns
  HTTP 202 with a running search-run ID.
- Missing, malformed, and incorrect bearer credentials return HTTP 401 with
  `WWW-Authenticate: Bearer`. The tracker has no role-based HTTP 403 case.
- `GET` and `POST /ebay/marketplace-account-deletion` are public eBay callback
  methods and intentionally do not use the tracker bearer token.
- `/review/parts`, `/review/progress`, `/review/dataset-readiness`,
  `/review/integrity`, `/review/queue`, `/review/listings/*`, and
  `/review/references/*` require the same bearer token. The `/review` HTML shell
  is public but contains no data or token.

Manual and scheduled scans share one lock per source. An overlap returns HTTP
409 with the active run ID. Feed discovery completeness controls misses;
detail-enrichment failures make a run partial without hiding listings observed
in complete feeds.

## Manual review and reference images

Open <http://localhost:8000/review>, enter `TRACKER_API_TOKEN`, and review the
active listing queue. The token stays in that browser tab's `sessionStorage`.
Human reviews are append-only and remain separate from deterministic matches;
they do not change scores or send Telegram messages.

Confirm, reject, or mark a listing uncertain. A confirmed listing can save
selected images as positive references. Negative references require a target
part. The tracker accepts only HTTPS images from the exact marketplace image
hosts currently supported (`i.ebayimg.com` and `i.ss.com`), removes image
metadata, and stores lossless content-addressed WebP files under
`data/references/`. Historical listing images remain reviewable.

The review page tracks a configurable pilot using each listing's latest review.
Queue modes cover high- and low-confidence deterministic matches, unmatched
broad candidates, confirmed listings needing positive images, parts needing
negative images, and uncertain decisions needing another review. The page
shows why each listing was selected. Filters remain in `sessionStorage` for the
current browser tab.

Use these outcomes consistently:

- **Confirmed:** the selected part is identifiable from a visible part number,
  catalogue comparison, known shape, or known donor-car evidence.
- **Rejected:** the listing is unrelated, wrong-model, pre-facelift where
  incompatible, replica, ordinary OEM, or does not show the target part.
- **Uncertain:** the image angle, resolution, obstruction, or evidence is not
  sufficient for a defensible decision.

Reference images can record view, fitted/removed/catalogue context, quality,
and obstruction. Positive references require the latest review to confirm the
same part. Negative references require an explicit target part. Before saving
any reference, the reviewer must confirm that the selected pixels do not
visibly contain seller contact information. This is a human check, not OCR or
proof that pixels contain no personal information.

The default advisory guidance is three confirmed listings, five positive
references, five distinct negative listings, and ten negative references per
part. Reaching the campaign or coverage target does not activate visual
matching or alerts.

Run `tracker review doctor` to validate review relationships, configured part
IDs, current image synchronization, reference paths, hashes, WebP decoding,
dimensions, label conflicts, privacy confirmations, and seller-derived note
text. It does not delete records. `--repair` removes only temporary reference
files older than one hour.

`tracker review export` writes active, sanitized references to a deterministic
part/label directory with JSON and CSV manifests plus SHA-256 checksums. The
export omits titles, URLs, marketplace external IDs, seller information,
notes, notification data, and credentials. Integrity errors block export by
default; `--allow-integrity-errors` still skips every invalid reference file.

Metadata removal cannot erase names, phone numbers, email addresses, or other
seller information visibly embedded in image pixels. Do not approve an image
that contains contact information. The tracker does not run OCR or automatic
pixel redaction. An eBay deletion notice clears associated review/reference
notes but retains sanitized image pixels, labels, part assignments, and listing
provenance under the selected retention policy.

Keep this page private. If a tunnel or reverse proxy is used for eBay
compliance, expose only `/ebay/marketplace-account-deletion`; never expose
`/review` or its API routes.

## eBay Production compliance

Production eBay access requires Marketplace Account Deletion notifications.
Expose this exact application route through an HTTPS reverse proxy:

```text
https://tracker.example.com/ebay/marketplace-account-deletion
```

Set the identical URL in `TRACKER_EBAY_DELETION_ENDPOINT_URL` and in the eBay
developer portal. Generate a 32–80 character token containing only letters,
digits, `_`, or `-`, store it in
`TRACKER_EBAY_DELETION_VERIFICATION_TOKEN`, and enter the same value in the
portal. The URL text must match exactly because it participates in eBay's
challenge hash. Production configuration rejects HTTP, localhost, and literal
non-public IP addresses.

The GET callback returns eBay's SHA-256 challenge response. The POST callback
validates the JSON, verifies `X-EBAY-SIGNATURE` with eBay's environment-specific
public key, durably reserves the notification, and returns `204`. Invalid
signatures return `412`; temporary OAuth or key failures return `503` so eBay
can retry. The callback is not protected by `TRACKER_API_TOKEN`.

An application-owned worker anonymizes matching seller identity, feedback, and
location in listings, snapshots, source metadata, and stored alert payloads.
Temporary deletion identifiers are erased after processing. Historical
listings marked as anonymized cannot regain seller data on later scans; the
tracker does not retain a seller-wide tombstone for hypothetical new listings.

For local Sandbox testing, use:

```dotenv
TRACKER_EBAY_ENVIRONMENT=sandbox
TRACKER_EBAY_ENABLED=true
TRACKER_EBAY_DELETION_ENDPOINT_URL=http://localhost:8000/ebay/marketplace-account-deletion
TRACKER_EBAY_DELETION_VERIFICATION_TOKEN=replace_with_32_to_80_safe_characters
```

Local HTTP verifies challenge behavior only. eBay portal test notifications
still require a publicly reachable HTTPS endpoint. Use the portal's **Send Test
Notification** action after the challenge succeeds.

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
- `/health` returns 503: the database failed, a scheduler/deletion worker
  stopped, Production eBay lacks a ready deletion callback, or deletion work is
  older than the configured threshold.
- Scan returns 400: the source is disabled, unconfigured, or unsupported.
- Scan returns 409: that source already has an active run.
- SS.com run is partial: inspect `/search-runs/{id}` and logs for a failed feed,
  detail page, content-type rejection, redirect rejection, or exhausted detail
  budget.
- No Telegram alerts: verify both Telegram values and run
  `docker compose exec irmscher-tracker tracker test-notification`.
- Review or reference problems: run `tracker review doctor`. Use `--repair`
  only to remove stale temporary files; database rows and WebP references are
  never automatically deleted.

Kleinanzeigen, Allegro, Ovoko, image embeddings, automatic visual recognition,
OCR, CAPTCHA
handling, proxies, authentication automation, purchasing, and seller contact
are out of scope.
