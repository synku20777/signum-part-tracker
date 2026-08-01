from __future__ import annotations

import asyncio
import logging
import sys

import typer
import uvicorn
from rich.console import Console
from rich.logging import RichHandler

app = typer.Typer(name="tracker", help="Irmscher Parts Tracker CLI")

console = Console()

def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(rich_tracebacks=True)],
    )

@app.command("trigger-scan")
def trigger_scan() -> None:
    """Trigger a search scan via the API."""
    setup_logging()
    import httpx

    from irmscher_tracker.settings import get_settings
    settings = get_settings()

    headers = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
    try:
        console.print("[yellow]Triggering eBay search via API...[/yellow]")
        resp = httpx.post("http://127.0.0.1:8000/runs/ebay", headers=headers, timeout=120.0)
        resp.raise_for_status()
        console.print("[green]Scan completed successfully[/green]")
        console.print(resp.json())
    except httpx.HTTPStatusError as e:
        console.print(f"[red]API Error ({e.response.status_code}):[/red] {e.response.text}")
        raise typer.Exit(code=1)
    except httpx.RequestError as e:
        console.print(f"[red]Failed to connect to API:[/red] {e}")
        console.print("[yellow]Is the tracker container running?[/yellow]")
        raise typer.Exit(code=1)

@app.command("serve")
def serve(
    host: str = typer.Option("0.0.0.0", help="Bind host"),
    port: int = typer.Option(8000, help="Bind port"),
) -> None:
    """Start the FastAPI server."""
    setup_logging()
    uvicorn.run(
        "irmscher_tracker.api.app:create_app",
        host=host,
        port=port,
        factory=True,
    )

@app.command("doctor")
def doctor() -> None:
    """Check system health and configuration."""
    setup_logging()
    import os

    import httpx

    from irmscher_tracker.settings import get_settings

    settings = get_settings()
    console.print("[bold]System Health Check[/bold]")

    # Check DB
    db_path = settings.database_url.replace("sqlite+aiosqlite:///", "")
    if os.path.exists(db_path):
        console.print(f"✅ Database found at {db_path}")
    else:
        console.print(f"❌ Database missing at {db_path} (Run 'tracker db upgrade'?)")

    # Check API
    try:
        resp = httpx.get("http://127.0.0.1:8000/health", timeout=5.0)
        if resp.status_code == 200:
            console.print(f"✅ API responding (v{resp.json()['version']})")
        else:
            console.print(f"❌ API returned status {resp.status_code}")
    except httpx.RequestError:
        console.print("❌ API not reachable")

    # Check parts.yaml
    if os.path.exists(settings.parts_config_path):
        console.print(f"✅ parts config found at {settings.parts_config_path}")
    else:
        console.print(f"❌ parts config missing at {settings.parts_config_path}")

@app.command("backup")
def backup(dest: str = typer.Argument(..., help="Destination sqlite file")) -> None:
    """Safely backup the SQLite database using VACUUM INTO."""
    setup_logging()
    import os
    import sqlite3

    from irmscher_tracker.settings import get_settings

    settings = get_settings()
    db_path = settings.database_url.replace("sqlite+aiosqlite:///", "")

    if not os.path.exists(db_path):
        console.print(f"[red]Database not found at {db_path}[/red]")
        raise typer.Exit(1)

    if os.path.exists(dest):
        console.print(f"[red]Destination {dest} already exists[/red]")
        raise typer.Exit(1)

    try:
        console.print(f"[yellow]Backing up database to {dest}...[/yellow]")
        with sqlite3.connect(db_path) as conn:
            conn.execute(f"VACUUM INTO '{dest}'")
        console.print(f"[green]Backup successful: {dest}[/green]")
    except Exception as e:
        console.print(f"[red]Backup failed:[/red] {e}")
        raise typer.Exit(1)

db_app = typer.Typer(help="Database commands")
app.add_typer(db_app, name="db")

@db_app.command("upgrade")
def db_upgrade() -> None:
    """Run database migrations."""
    setup_logging()
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True, text=True,
    )
    if result.stdout:
        console.print(result.stdout)
    if result.returncode != 0:
        console.print(f"[red]Migration failed:[/red] {result.stderr}")
        raise typer.Exit(code=1)
    console.print("[green]Database upgraded successfully[/green]")

@app.command("test-notification")
def test_notification() -> None:
    """Send a test Telegram notification."""
    setup_logging()
    from irmscher_tracker.notifications.telegram import TelegramNotifier
    from irmscher_tracker.settings import get_settings

    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        console.print("[red]Error: Telegram credentials not configured[/red]")
        raise typer.Exit(code=1)

    async def _test() -> bool:
        notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        try:
            return await notifier.send_test_message()
        finally:
            await notifier.close()

    success = asyncio.run(_test())
    if success:
        console.print("[green]Test notification sent successfully![/green]")
    else:
        console.print("[red]Failed to send test notification[/red]")
        raise typer.Exit(code=1)

if __name__ == "__main__":
    app()
