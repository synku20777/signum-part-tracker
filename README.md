# Irmscher Parts Tracker

The Irmscher Parts Tracker is an automated system designed to monitor online marketplaces (such as eBay) for specific Opel Signum Irmscher car parts. It searches for parts based on defined criteria, evaluates listings using a deterministic scoring system, stores the results in a PostgreSQL database, and sends alerts via Telegram for new or updated listings.

## Features

- **eBay Browse API integration:** Connects to eBay to fetch the latest listings securely and efficiently.
- **Deterministic part matching with scoring:** Uses an intelligent scoring mechanism based on titles, categories, condition, and price to filter out irrelevant items.
- **Telegram notifications:** Real-time push alerts to your Telegram device for new parts or price drops.
- **PostgreSQL persistence:** Robust data storage for items, price history, and configuration.
- **Docker Compose deployment:** Fully containerized for easy and reproducible deployments.
- **FastAPI REST API:** Manage parts configuration and view items through a modern HTTP API.
- **CLI management:** Command-line interface for manual execution, migrations, and testing.

## Quick Start

You can run the application locally using [`uv`](https://github.com/astral-sh/uv).

```bash
# Sync dependencies
uv sync

# Create your .env file
cp .env.example .env

# Edit .env with your credentials (see Configuration section)
nano .env

# Run database migrations
uv run tracker db upgrade

# Run an initial manual eBay search
uv run tracker run ebay
```

## eBay Developer Credentials

To use the eBay source adapter, you need to set up eBay Developer credentials:

1. Go to the [eBay Developer Program](https://developer.ebay.com) and sign in or create an account.
2. Navigate to **Application Keys** and create a new application.
3. Obtain your **Client ID** and **Client Secret** (App ID and Cert ID) for the Production environment.
4. Go to **User Tokens** and generate an application access token to ensure your Browse API access is configured. Note that the application currently uses client credentials grant directly, so you just need the ID and Secret.
5. Set `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` in your `.env` file.

## Telegram Bot Setup

To receive notifications via Telegram:

1. Open Telegram and search for `@BotFather`.
2. Send `/newbot` and follow the instructions to create a new bot.
3. Copy the **token** provided by BotFather.
4. To get your **Chat ID**, send a message to your new bot, then forward that message to `@userinfobot` or check `https://api.telegram.org/bot<YourBOTToken>/getUpdates`.
5. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in your `.env` file.

## Configuration

The application is configured using environment variables. You can set them in a `.env` file or directly in your environment.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` | Connection string for PostgreSQL database |
| `EBAY_CLIENT_ID` | (required) | eBay Developer App ID |
| `EBAY_CLIENT_SECRET` | (required) | eBay Developer Cert ID |
| `EBAY_MARKETPLACE_ID` | `EBAY_DE` | Marketplace to search (e.g., EBAY_DE, EBAY_GB) |
| `TELEGRAM_BOT_TOKEN` | (required) | Token for Telegram bot |
| `TELEGRAM_CHAT_ID` | (required) | Target chat ID for Telegram notifications |
| `SCORE_THRESHOLD` | `50` | Minimum score (0-100) for a part to be considered a match |
| `PRICE_CHANGE_PERCENT`| `5.0` | Minimum percentage price change to trigger an alert |
| `SEARCH_INTERVAL_MINUTES` | `30` | Interval for the background scheduler |
| `LOG_LEVEL` | `INFO` | Application log level (DEBUG, INFO, WARNING, ERROR) |
| `PARTS_CONFIG_PATH` | `config/parts.yaml` | Path to the parts definitions file |

## Database Migrations

Alembic is used to manage database schema changes.

```bash
# Upgrade database to the latest schema
uv run tracker db upgrade

# Alternative using alembic directly:
uv run alembic upgrade head

# Create a new migration after making model changes:
uv run alembic revision --autogenerate -m "description_of_changes"
```

## CLI Usage

The system is managed via a Command Line Interface (CLI):

```bash
# Run a one-off search on eBay
uv run tracker run ebay

# Run a search on all configured sources
uv run tracker run all

# Start the FastAPI REST API
uv run tracker serve

# Start the background task scheduler
uv run tracker scheduler

# Apply database migrations
uv run tracker db upgrade

# Send a test notification to verify Telegram setup
uv run tracker test-notification
```

## API Endpoints

The system provides a REST API via FastAPI. When running with `tracker serve`, it defaults to `http://localhost:8000`.

| Method | Endpoint | Description | Example |
|---|---|---|---|
| `GET` | `/health` | Application healthcheck | `curl -X GET http://localhost:8000/health` |
| `GET` | `/api/v1/items` | List discovered items | `curl -X GET http://localhost:8000/api/v1/items` |
| `GET` | `/api/v1/items/{item_id}` | Get specific item details | `curl -X GET http://localhost:8000/api/v1/items/1` |
| `GET` | `/api/v1/config/parts` | Get active search config | `curl -X GET http://localhost:8000/api/v1/config/parts` |

*Note: Access Swagger UI documentation at `http://localhost:8000/docs`.*

## Docker Compose Deployment

The application includes a `docker-compose.yml` for running the database, API, and scheduler components easily.

```bash
# Set up configuration
cp .env.example .env
nano .env # configure all required variables

# Start services in the background
docker compose up -d

# Check logs
docker compose logs -f
```

## CasaOS Deployment

The Irmscher Parts Tracker is designed to be easily deployable on CasaOS or similar Docker management dashboards:

1. CasaOS supports Docker Compose natively via its App installer.
2. You can upload the `docker-compose.yml` via the CasaOS dashboard ("Install a customized app" -> "Import" -> "Docker Compose").
3. Set your environment variables in the CasaOS UI according to your `.env` setup.
4. Make sure volume mounts (`pgdata` and `config/parts.yaml`) are correctly mapped for persistence.
5. The provided Docker images are multi-architecture, supporting ARM64 for Raspberry Pi / mini PCs commonly used with CasaOS.

## Backup and Restore

### Database Backup
Use `pg_dump` to create a logical backup of the PostgreSQL database.
```bash
docker compose exec postgres pg_dump -U tracker -d irmscher_tracker -F c -f /tmp/backup.dump
docker compose cp postgres:/tmp/backup.dump ./backup.dump
```

### Database Restore
Use `pg_restore` to restore the backup.
```bash
docker compose cp ./backup.dump postgres:/tmp/backup.dump
docker compose exec postgres pg_restore -U tracker -d irmscher_tracker -1 /tmp/backup.dump
```

## Troubleshooting

- **eBay authentication errors:** Check your client ID and secret. Ensure they are for the Production environment and that the application is active in the eBay Developer portal.
- **Telegram delivery issues:** Run `uv run tracker test-notification`. Ensure the bot token is correct, the bot has not been blocked by the user, and the chat ID is an integer (including `-` for groups).
- **Database connection problems:** Verify the `DATABASE_URL` matches your local/Docker PostgreSQL credentials. For Docker, ensure the `postgres` service is healthy.
- **Docker networking:** Make sure `tracker-api` and `tracker-scheduler` can reach `postgres:5432`.
- **Rate limiting:** The eBay Browse API has strict rate limits. Ensure `SEARCH_INTERVAL_MINUTES` is not set too low.

## Adding a New Source Adapter

The system is designed to be extensible. To add a new source (e.g., Kleinanzeigen, Allegro):

1. Create a new file in `src/irmscher_tracker/sources/` (e.g., `kleinanzeigen.py`).
2. Implement the `SourceAdapter` Abstract Base Class.
3. Add the new source to the `Source` enum in `src/irmscher_tracker/models/core.py`.
4. Register the new source in `src/irmscher_tracker/services/search.py`.
5. Add a CLI command in `src/irmscher_tracker/cli.py` (e.g., `tracker run kleinanzeigen`).
6. Write integration tests in `tests/sources/`.

## Development

Set up your development environment with testing and linting tools:

```bash
# Install development dependencies
uv sync --dev

# Run type checker
uv run mypy src

# Run linter
uv run ruff check .

# Format code
uv run ruff format .

# Run test suite
uv run pytest
```

## Project Structure

```text
signum-part-tracker/
├── alembic/                  # Database migration scripts
├── config/                   # Configuration files (parts.yaml)
├── src/
│   └── irmscher_tracker/
│       ├── api/              # FastAPI endpoints
│       ├── models/           # SQLAlchemy and Pydantic models
│       ├── services/         # Core business logic (Search, Notifications)
│       ├── sources/          # Adapters (eBay, etc.)
│       ├── cli.py            # Command Line Interface
│       ├── db.py             # Database session management
│       └── scheduler.py      # Background task runner
├── tests/                    # Pytest test suite
├── .env.example              # Example environment variables
├── .dockerignore
├── alembic.ini               # Alembic configuration
├── docker-compose.yml        # Multi-container orchestration
├── Dockerfile                # Production container image
├── pyproject.toml            # Project metadata and dependencies
├── README.md                 # Project documentation
└── uv.lock                   # Dependency lockfile
```

## License

This project is licensed under the MIT License - see the LICENSE file for details.
