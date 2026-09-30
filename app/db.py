"""Database engine and session management."""
from __future__ import annotations

import logging
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config

log = logging.getLogger("timing.db")


class Base(DeclarativeBase):
    pass


engine = create_engine(config.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI dependency: one session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db_with_retry() -> None:
    """Create tables, retrying until PostgreSQL accepts connections."""
    from . import models  # noqa: F401  (register metadata)

    last_exc: Exception | None = None
    for attempt in range(1, config.DB_CONNECT_RETRIES + 1):
        try:
            Base.metadata.create_all(engine)
            log.info("database schema ready")
            return
        except Exception as exc:  # noqa: BLE001 - retry on any connection failure
            last_exc = exc
            log.warning(
                "database not ready (attempt %d/%d): %s",
                attempt,
                config.DB_CONNECT_RETRIES,
                exc,
            )
            time.sleep(config.DB_CONNECT_RETRY_DELAY_SECONDS)
    raise RuntimeError(f"could not initialize database: {last_exc}")
