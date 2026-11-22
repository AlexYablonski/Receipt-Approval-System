"""Database engine and sessions."""

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


def create_db_engine(url: str) -> Engine:
    is_sqlite = url.startswith("sqlite")
    # Background tasks run in other threads. SQLite ties a connection to the thread that
    # created it unless this is off. Safe here because each task uses its own session.
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    engine = create_engine(url, connect_args=connect_args)

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection, connection_record):
            # SQLite ignores foreign keys unless this is switched on for every new connection.
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


engine = create_db_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request. Background tasks open their own with SessionLocal()."""
    with SessionLocal() as session:
        yield session
