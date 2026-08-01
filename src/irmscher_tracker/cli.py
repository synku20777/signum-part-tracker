from __future__ import annotations

import asyncio
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import typer
import uvicorn
from rich.console import Console
from rich.logging import RichHandler

from irmscher_tracker.settings import Settings, get_settings

app = typer.Typer(name="tracker", help="Irmscher Parts Tracker CLI")
db_app = typer.Typer(help="Database commands")
app.add_typer(db_app, name="db")
console = Console()


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(rich_tracebacks=True)],
    )


def _database_path(settings: Settings) -> Path:
    prefix = "sqlite+aiosqlite:///"
    if not settings.database_url.startswith(prefix):
        raise typer.BadParameter("Backup and restore require a SQLite database")
    return Path(settings.database_url.removeprefix(prefix)).resolve()


def _data_file(value: str, database_path: Path) -> Path:
    path = Path(value).resolve()
    if path.parent != database_path.parent:
        raise typer.BadParameter("Path must be directly inside the tracker data directory")
    return path


@app.command("trigger-scan")
def trigger_scan(source: str = typer.Argument("ebay")) -> None:
    """Trigger a source scan through the authenticated API."""
    if source != "ebay":
        raise typer.BadParameter("Only the ebay source is implemented")
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    try:
        response = httpx.post(
            f"http://127.0.0.1:{settings.api_port}/runs/ebay",
            headers=headers,
            timeout=120.0,
        )
        response.raise_for_status()
        console.print(response.json())
    except httpx.HTTPStatusError as exc:
        console.print(f"API error ({exc.response.status_code}): {exc.response.text}")
        raise typer.Exit(code=1) from exc
    except httpx.RequestError as exc:
        console.print(f"Failed to connect to API: {exc}")
        raise typer.Exit(code=1) from exc


@app.command("serve")
def serve(
    host: str = typer.Option("0.0.0.0", help="Bind host"),
    port: int = typer.Option(8000, help="Bind port"),
) -> None:
    setup_logging()
    uvicorn.run(
        "irmscher_tracker.api.app:create_app",
        host=host,
        port=port,
        factory=True,
    )


@app.command("doctor")
def doctor() -> None:
    """Check the database, config file, API, and optional integrations."""
    settings = get_settings()
    database_path = _database_path(settings)
    checks = {
        "database": database_path.exists(),
        "parts config": settings.parts_config_path.exists(),
        "eBay": bool(settings.ebay_client_id and settings.ebay_client_secret.get_secret_value()),
        "Telegram": bool(
            settings.telegram_bot_token.get_secret_value() and settings.telegram_chat_id
        ),
    }
    try:
        response = httpx.get(f"http://127.0.0.1:{settings.api_port}/health", timeout=5.0)
        checks["API"] = response.status_code == 200
    except httpx.RequestError:
        checks["API"] = False
    for name, healthy in checks.items():
        console.print(f"{'OK' if healthy else 'MISSING'}: {name}")
    if not checks["database"] or not checks["parts config"] or not checks["API"]:
        raise typer.Exit(code=1)


@app.command("backup")
def backup(destination: str) -> None:
    """Create a consistent SQLite backup inside the data directory."""
    database_path = _database_path(get_settings())
    destination_path = _data_file(destination, database_path)
    if not database_path.exists():
        raise typer.BadParameter(f"Database not found at {database_path}")
    if destination_path.exists():
        raise typer.BadParameter(f"Destination already exists: {destination_path}")
    with sqlite3.connect(database_path) as source, sqlite3.connect(destination_path) as target:
        source.backup(target)
    console.print(f"Backup created: {destination_path}")


@app.command("restore")
def restore(source: str) -> None:
    """Restore a SQLite backup while the server is stopped."""
    database_path = _database_path(get_settings())
    source_path = _data_file(source, database_path)
    if not source_path.is_file():
        raise typer.BadParameter(f"Backup not found: {source_path}")
    if source_path == database_path:
        raise typer.BadParameter("Backup and database paths must differ")
    with sqlite3.connect(source_path) as backup_db, sqlite3.connect(database_path) as target:
        backup_db.backup(target)
    console.print(f"Database restored from: {source_path}")


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Run database migrations."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=False,
    )
    if result.returncode:
        raise typer.Exit(code=result.returncode)


@app.command("test-notification")
def test_notification() -> None:
    """Send a test Telegram notification."""
    from irmscher_tracker.notifications.telegram import TelegramNotifier

    settings = get_settings()
    token = settings.telegram_bot_token.get_secret_value()
    if not token or not settings.telegram_chat_id:
        raise typer.BadParameter("Telegram credentials are not configured")

    async def send() -> bool:
        notifier = TelegramNotifier(token, settings.telegram_chat_id)
        try:
            return await notifier.send_test_message()
        finally:
            await notifier.close()

    if not asyncio.run(send()):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
