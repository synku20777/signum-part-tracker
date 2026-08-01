from __future__ import annotations

import asyncio
import logging
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import typer
import uvicorn
from rich.console import Console
from rich.logging import RichHandler

from irmscher_tracker.settings import Settings, get_settings
from irmscher_tracker.sources.sscom import load_feed_urls

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
def trigger_scan(
    source: str = typer.Argument("ebay"),
    wait: bool = typer.Option(False, "--wait", help="Poll until the run finishes"),
) -> None:
    """Trigger a source scan through the authenticated API."""
    if source not in {"ebay", "sscom"}:
        raise typer.BadParameter("Source must be ebay or sscom")
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    try:
        response = httpx.post(
            f"http://127.0.0.1:{settings.api_port}/runs/{source}",
            headers=headers,
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        console.print(payload)
        if wait:
            _wait_for_run(settings, int(payload["search_run_id"]))
    except httpx.HTTPStatusError as exc:
        console.print(f"API error ({exc.response.status_code}): {exc.response.text}")
        raise typer.Exit(code=1) from exc
    except httpx.RequestError as exc:
        console.print(f"Failed to connect to API: {exc}")
        raise typer.Exit(code=1) from exc


def _wait_for_run(settings: Settings, run_id: int) -> None:
    terminal = {"completed", "partial", "failed", "interrupted", "cancelled"}
    while True:
        try:
            response = httpx.get(
                f"http://127.0.0.1:{settings.api_port}/search-runs/{run_id}", timeout=10.0
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            console.print(f"Failed to poll run {run_id}: {exc}")
            raise typer.Exit(code=1) from exc
        payload = response.json()
        if payload["status"] in terminal:
            console.print(payload)
            if payload["status"] in {"failed", "interrupted", "cancelled"}:
                raise typer.Exit(code=1)
            return
        time.sleep(2)


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
    try:
        sscom_configured = bool(load_feed_urls(settings.sources_config_path))
    except (OSError, ValueError):
        sscom_configured = False
    ebay_configured = bool(
        settings.ebay_client_id and settings.ebay_client_secret.get_secret_value()
    )
    checks = {
        "database": database_path.exists(),
        "parts config": settings.parts_config_path.exists(),
        "sources config": settings.sources_config_path.exists(),
        "Telegram": bool(
            settings.telegram_bot_token.get_secret_value() and settings.telegram_chat_id
        ),
    }
    try:
        response = httpx.get(f"http://127.0.0.1:{settings.api_port}/health", timeout=5.0)
        checks["API"] = response.status_code == 200
    except httpx.RequestError:
        checks["API"] = False
    source_states = {
        "eBay": (settings.ebay_enabled, ebay_configured),
        "SS.com": (settings.sscom_enabled, sscom_configured),
    }
    for name, healthy in checks.items():
        console.print(f"{'OK' if healthy else 'MISSING'}: {name}")
    for name, (enabled, configured) in source_states.items():
        console.print(
            f"{name}: enabled={'yes' if enabled else 'no'}, "
            f"configured={'yes' if configured else 'no'}, "
            f"ready={'yes' if enabled and configured else 'no'}"
        )
    core_ready = all(
        checks[name] for name in ("database", "parts config", "sources config", "API")
    )
    sources_ready = all(
        not enabled or configured for enabled, configured in source_states.values()
    )
    if not core_ready or not sources_ready:
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
