"""Lazy SQLAlchemy engine and session-factory construction."""

from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from project3_crm.config import DatabaseSettings, get_database_settings


def create_database_engine(settings: DatabaseSettings | None = None) -> Engine:
    """Create a lazy PostgreSQL Engine without opening a connection."""

    database_settings = settings or get_database_settings()
    return create_engine(
        database_settings.sqlalchemy_database_url,
        pool_pre_ping=True,
    )


@lru_cache
def get_database_engine() -> Engine:
    """Return the process Engine without connecting at import time."""

    return create_database_engine()


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a factory suitable for atomic ``Session.begin()`` blocks."""

    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def check_database_readiness(engine: Engine) -> None:
    """Verify database reachability with one read-only lightweight query."""

    with engine.connect() as connection:
        result = connection.execute(text("SELECT 1")).scalar_one()
    if result != 1:
        raise RuntimeError("database readiness query returned an invalid result")
