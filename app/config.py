"""Runtime configuration, overridable via environment variables."""
from __future__ import annotations

import os

# SQLAlchemy database URL. Defaults match docker-compose.yml.
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://timing:timing@localhost:5432/timing",
)

# Startup retry policy: the app waits for PostgreSQL to become ready.
DB_CONNECT_RETRIES: int = int(os.getenv("DB_CONNECT_RETRIES", "30"))
DB_CONNECT_RETRY_DELAY_SECONDS: float = float(os.getenv("DB_CONNECT_RETRY_DELAY_SECONDS", "1.0"))
