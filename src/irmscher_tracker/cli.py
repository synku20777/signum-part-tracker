from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from pathlib import Path

import httpx
import typer
import uvicorn
from rich.console import Console
from rich.logging import RichHandler

from irmscher_tracker.db.engine import get_session_factory
from irmscher_tracker.matcher import PartMatcher
from irmscher_tracker.services.backup import (
    BackupError,
    create_backup,
    restore_backup,
)
from irmscher_tracker.services.review import ReferenceImageStore, ReviewService
from irmscher_tracker.services.review_export import DatasetExportError, ReviewDatasetExporter
from irmscher_tracker.services.review_integrity import ReviewIntegrityService
from irmscher_tracker.settings import Settings, get_settings
from irmscher_tracker.sources.ebay_client import EbayEnvironment
from irmscher_tracker.sources.sscom import load_feed_urls
from irmscher_tracker.vision.alerts import VisionAlertService, VisualMatchNotFoundError
from irmscher_tracker.vision.image_loader import VisionImageLoader
from irmscher_tracker.vision.model import Dinov2Embedder
from irmscher_tracker.vision.service import (
    VisionDisabledError,
    VisionRunBusyError,
    VisionService,
)

app = typer.Typer(name="tracker", help="Irmscher Parts Tracker CLI")
db_app = typer.Typer(help="Database commands")
review_app = typer.Typer(help="Manual review commands")
vision_app = typer.Typer(help="CPU visual-similarity commands")
app.add_typer(db_app, name="db")
app.add_typer(review_app, name="review")
app.add_typer(vision_app, name="vision")
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
    matcher = PartMatcher(settings.parts_config_path)
    integrity = asyncio.run(
        ReviewIntegrityService(
            get_session_factory(settings.database_url),
            settings.data_directory,
            {part.id for part in matcher.parts},
        ).check()
    )
    checks["review integrity"] = integrity.status != "error"
    health_payload: dict[str, object] = {}
    try:
        response = httpx.get(f"http://127.0.0.1:{settings.api_port}/health", timeout=5.0)
        checks["API"] = response.status_code == 200
        health_payload = response.json()
    except (httpx.RequestError, ValueError):
        checks["API"] = False
    source_states = {
        "eBay": (settings.ebay_enabled, ebay_configured),
        "SS.com": (settings.sscom_enabled, sscom_configured),
    }
    for name, healthy in checks.items():
        console.print(f"{'OK' if healthy else 'MISSING'}: {name}")
    console.print(
        f"Review integrity: status={integrity.status}, "
        f"errors={integrity.summary.errors}, warnings={integrity.summary.warnings}"
    )
    for name, (enabled, configured) in source_states.items():
        console.print(
            f"{name}: enabled={'yes' if enabled else 'no'}, "
            f"configured={'yes' if configured else 'no'}, "
            f"ready={'yes' if enabled and configured else 'no'}"
        )
    deletion_configured = settings.ebay_deletion_callback_configured
    deletion_locally_ready = settings.ebay_deletion_callback_ready
    deletion_worker = str(health_payload.get("ebay_deletion_worker", "unknown"))
    deletion_pending = health_payload.get("ebay_deletion_pending", "unknown")
    deletion_oldest = health_payload.get("ebay_deletion_oldest_pending_seconds")
    console.print(
        "eBay deletion callback: "
        f"environment={settings.ebay_environment.value}, "
        f"configured={'yes' if deletion_configured else 'no'}, "
        f"ready={'yes' if deletion_locally_ready and deletion_worker == 'running' else 'no'}, "
        f"worker={deletion_worker}, pending={deletion_pending}, "
        f"oldest_pending_seconds={deletion_oldest}"
    )
    core_ready = all(
        checks[name]
        for name in ("database", "parts config", "sources config", "API", "review integrity")
    )
    sources_ready = all(
        not enabled or configured for enabled, configured in source_states.values()
    )
    deletion_ready = not (
        settings.ebay_enabled
        and settings.ebay_environment is EbayEnvironment.PRODUCTION
        and (not deletion_locally_ready or deletion_worker != "running")
    )
    if not core_ready or not sources_ready or not deletion_ready:
        raise typer.Exit(code=1)


@review_app.command("doctor")
def review_doctor(
    repair: bool = typer.Option(False, "--repair", help="Remove stale temporary files"),
) -> None:
    """Validate review records and reference storage."""
    settings = get_settings()
    matcher = PartMatcher(settings.parts_config_path)
    result = asyncio.run(
        ReviewIntegrityService(
            get_session_factory(settings.database_url),
            settings.data_directory,
            {part.id for part in matcher.parts},
        ).check(repair=repair)
    )
    for check in result.checks:
        console.print(
            f"{check.status.upper()}: {check.name} "
            f"affected={check.affected_count} repairable={'yes' if check.repairable else 'no'}"
        )
    console.print(
        f"Review integrity: status={result.status}, errors={result.summary.errors}, "
        f"warnings={result.summary.warnings}"
    )
    if result.status == "error":
        raise typer.Exit(code=1)


@review_app.command("export")
def review_export(
    output_directory: str | None = typer.Argument(None),
    allow_integrity_errors: bool = typer.Option(False, "--allow-integrity-errors"),
) -> None:
    """Export active sanitized reference images and deterministic manifests."""
    settings = get_settings()
    matcher = PartMatcher(settings.parts_config_path)
    session_factory = get_session_factory(settings.database_url)
    destination = (
        Path(output_directory)
        if output_directory
        else Path("exports") / f"dataset_{time.strftime('%Y%m%d_%H%M%S')}"
    )
    if not destination.is_absolute():
        destination = settings.data_directory / destination
    store = ReferenceImageStore(settings.data_directory)
    review_service = ReviewService(session_factory, matcher, store, settings)
    integrity_service = ReviewIntegrityService(
        session_factory,
        settings.data_directory,
        {part.id for part in matcher.parts},
    )

    async def run() -> Path:
        try:
            return await ReviewDatasetExporter(
                session_factory,
                settings.data_directory,
                settings.parts_config_path,
                matcher,
                review_service,
                integrity_service,
            ).export(destination, allow_integrity_errors=allow_integrity_errors)
        finally:
            await store.close()

    try:
        created = asyncio.run(run())
    except DatasetExportError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Dataset exported: {created}")


def _vision_service(settings: Settings) -> tuple[VisionService, ReferenceImageStore]:
    matcher = PartMatcher(settings.parts_config_path)
    store = ReferenceImageStore(settings.data_directory)
    session_factory = get_session_factory(settings.database_url)
    integrity = ReviewIntegrityService(
        session_factory,
        settings.data_directory,
        {part.id for part in matcher.parts},
    )
    return (
        VisionService(
            session_factory,
            settings,
            [part.id for part in matcher.parts],
            store,
            Dinov2Embedder.for_settings(settings),
            integrity,
        ),
        store,
    )


async def _reserve_vision(service: VisionService, run_type: str) -> int:
    try:
        return await service.reserve(run_type)  # type: ignore[arg-type]
    except VisionDisabledError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except VisionRunBusyError as exc:
        console.print(f"Vision run already active: {exc.run_id}")
        raise typer.Exit(code=1) from exc


@vision_app.command("warmup")
def vision_warmup() -> None:
    """Download/load DINOv2 and run one generated test image."""
    settings = get_settings()
    service, store = _vision_service(settings)

    async def run() -> None:
        try:
            run_id = await _reserve_vision(service, "warmup")
            await service.warmup(run_id)
            row = await service.run(run_id)
            if row is None or row.status != "completed":
                raise typer.Exit(code=1)
            embedder = service.embedder
            console.print(f"Model ID: {embedder.model_id}")
            console.print(f"Resolved revision: {embedder.resolved_revision}")
            console.print(f"Embedding dimension: {embedder.embedding_dimension}")
            console.print(f"Load time: {embedder.load_time_seconds:.3f}s")
            console.print(f"Inference time: {embedder.last_inference_seconds:.3f}s")
            console.print(f"Model cache: {settings.vision_model_cache_directory}")
        finally:
            service.embedder.release()
            await store.close()

    asyncio.run(run())


@vision_app.command("rebuild-references")
def vision_rebuild_references(
    force: bool = typer.Option(False, "--force", help="Replace current-model embeddings"),
) -> None:
    """Embed all active approved reference images."""
    settings = get_settings()
    service, store = _vision_service(settings)

    async def run() -> None:
        try:
            run_id = await _reserve_vision(service, "reference_rebuild")
            await service.rebuild_references(run_id, force=force)
            row = await service.run(run_id)
            assert row is not None
            console.print(
                f"Reference run {row.id}: {row.status}; processed={row.processed_count}, "
                f"skipped={row.skipped_count}, failed={row.failed_count}"
            )
            if row.status == "failed":
                raise typer.Exit(code=1)
        finally:
            service.embedder.release()
            await store.close()

    asyncio.run(run())


@vision_app.command("scan")
def vision_scan(
    limit: int | None = typer.Option(None, "--limit", min=1, max=500),
    source: str | None = typer.Option(None, "--source"),
    listing_id: int | None = typer.Option(None, "--listing-id", min=1),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Analyze a bounded set of current marketplace listing images."""
    if source not in {None, "ebay", "sscom"}:
        raise typer.BadParameter("Source must be ebay or sscom")
    settings = get_settings()
    service, store = _vision_service(settings)

    async def run() -> None:
        try:
            run_id = await _reserve_vision(service, "listing_scan")
            await service.scan(
                run_id,
                limit=limit,
                source=source,
                listing_id=listing_id,
                force=force,
            )
            row = await service.run(run_id)
            assert row is not None
            console.print(
                f"Vision scan {row.id}: {row.status}; requested={row.requested_count}, "
                f"processed={row.processed_count}, failed={row.failed_count}"
            )
            if row.status == "failed":
                raise typer.Exit(code=1)
        finally:
            service.embedder.release()
            await store.close()

    asyncio.run(run())


@vision_app.command("evaluate")
def vision_evaluate() -> None:
    """Evaluate persisted visual retrieval against latest manual reviews."""
    settings = get_settings()
    service, store = _vision_service(settings)

    async def run() -> None:
        try:
            run_id = await _reserve_vision(service, "evaluation")
            result = await service.evaluate(run_id)
            if result is None:
                raise typer.Exit(code=1)
            report, json_path, csv_path = result
            console.print(
                f"Evaluated {report['total_evaluated_listings']} listings; "
                f"top-1={report['top_1_accuracy']}, top-3={report['top_3_recall']}, "
                f"MRR={report['mean_reciprocal_rank']}"
            )
            console.print(f"JSON: {json_path}")
            console.print(f"CSV: {csv_path}")
        finally:
            service.embedder.release()
            await store.close()

    asyncio.run(run())


@vision_app.command("status")
def vision_status() -> None:
    """Show vision enablement, cache, and active-run state."""
    settings = get_settings()
    service, store = _vision_service(settings)

    async def run() -> None:
        try:
            console.print(await service.status())
        finally:
            await store.close()

    asyncio.run(run())


@vision_app.command("alert-preview")
def vision_alert_preview(
    match_id: int,
    send: bool = typer.Option(False, "--send", help="Explicitly send this test preview"),
) -> None:
    """Print or explicitly send an experimental visual-candidate preview."""
    settings = get_settings()
    matcher = PartMatcher(settings.parts_config_path)
    store = ReferenceImageStore(settings.data_directory)
    alerts = VisionAlertService(
        get_session_factory(settings.database_url),
        VisionImageLoader(store),
        {part.id: part.name for part in matcher.parts},
    )

    async def run() -> None:
        notifier = None
        try:
            if send:
                token = settings.telegram_bot_token.get_secret_value()
                if not settings.telegram_enabled or not token or not settings.telegram_chat_id:
                    raise typer.BadParameter("Telegram credentials are not configured")
                from irmscher_tracker.notifications.telegram import TelegramNotifier

                notifier = TelegramNotifier(token, settings.telegram_chat_id)
                preview = await alerts.send(match_id, notifier)
            else:
                preview = await alerts.preview(match_id)
            console.print(preview.text())
        except VisualMatchNotFoundError as exc:
            raise typer.BadParameter(str(exc)) from exc
        finally:
            if notifier is not None:
                await notifier.close()
            await store.close()

    asyncio.run(run())


@app.command("backup")
def backup(destination: str) -> None:
    """Create a full archive, or a legacy database-only .sqlite backup."""
    settings = get_settings()
    database_path = _database_path(settings)
    destination_path = _data_file(destination, database_path)
    if not database_path.exists():
        raise typer.BadParameter(f"Database not found at {database_path}")
    if destination_path.exists():
        raise typer.BadParameter(f"Destination already exists: {destination_path}")
    try:
        create_backup(database_path, settings.data_directory, destination_path)
    except BackupError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Backup created: {destination_path}")


@app.command("restore")
def restore(source: str) -> None:
    """Restore a full archive or legacy SQLite backup while stopped."""
    settings = get_settings()
    database_path = _database_path(settings)
    source_path = _data_file(source, database_path)
    if not source_path.is_file():
        raise typer.BadParameter(f"Backup not found: {source_path}")
    if source_path == database_path:
        raise typer.BadParameter("Backup and database paths must differ")
    try:
        restore_backup(source_path, database_path, settings.data_directory)
    except BackupError as exc:
        raise typer.BadParameter(str(exc)) from exc
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
