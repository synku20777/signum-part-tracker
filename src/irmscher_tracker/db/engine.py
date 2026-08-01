"""Async SQLAlchemy engine and session factory management.

The module-level singletons are used by the CLI and API, but callers
can also create one-off engines by passing a URL directly to the
factory functions.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engines: dict[str, AsyncEngine] = {}
_factories: dict[str, async_sessionmaker[AsyncSession]] = {}


def get_engine(database_url: str) -> AsyncEngine:
    """Return (or create) an ``AsyncEngine`` for *database_url*."""
    if database_url not in _engines:
        connect_args: dict[str, bool] = {}
        if database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False

        engine = create_async_engine(
            database_url,
            echo=False,
            connect_args=connect_args,
        )

        if database_url.startswith("sqlite"):
            import sqlite3

            from sqlalchemy import event

            @event.listens_for(engine.sync_engine, "connect")
            def _set_sqlite_pragma(dbapi_connection, connection_record):
                if isinstance(dbapi_connection, sqlite3.Connection):
                    cursor = dbapi_connection.cursor()
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.execute("PRAGMA foreign_keys=ON")
                    cursor.execute("PRAGMA busy_timeout=10000")
                    cursor.close()

        _engines[database_url] = engine
    return _engines[database_url]


def get_session_factory(
    database_url: str,
) -> async_sessionmaker[AsyncSession]:
    """Return (or create) a session factory bound to *database_url*."""
    if database_url not in _factories:
        engine = get_engine(database_url)
        _factories[database_url] = async_sessionmaker(
            engine, expire_on_commit=False
        )
    return _factories[database_url]


async def reset_engine(database_url: str | None = None) -> None:
    """Dispose engines and clear caches.

    If *database_url* is given, only that engine is disposed.
    Otherwise **all** cached engines are disposed.
    """
    if database_url is not None:
        engine = _engines.pop(database_url, None)
        _factories.pop(database_url, None)
        if engine is not None:
            await engine.dispose()
    else:
        for eng in _engines.values():
            await eng.dispose()
        _engines.clear()
        _factories.clear()
