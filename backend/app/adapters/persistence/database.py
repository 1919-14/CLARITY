"""SQLAlchemy engine, session management, and Base declarative model for CLARITY.

Provides clean engine creation, scoped session factory, and transaction context managers.
Supports MySQL with PyMySQL in production/development, and SQLite for fast, isolated tests.
"""

import os
from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session, Session

# Declarative Base for all persistent models
Base = declarative_base()


class DatabaseManager:
    """Manages SQLAlchemy engine lifecycle and session factories."""

    def __init__(self, database_uri: str, echo: bool = False):
        env = os.environ.get("FLASK_ENV", os.environ.get("ENV", "development")).lower()
        if env == "production" and database_uri.startswith("sqlite"):
            raise ValueError(
                "SQLite database URI cannot be used in production environment. "
                "A production database (e.g., MySQL) URL must be configured."
            )

        self.database_uri = database_uri
        self.echo = echo

        # Configure connection pool settings appropriately for SQLite vs MySQL
        connect_args = {}
        if database_uri.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            kwargs = {}
            if ":memory:" in database_uri:
                from sqlalchemy.pool import StaticPool
                kwargs["poolclass"] = StaticPool

            self.engine = create_engine(
                database_uri,
                connect_args=connect_args,
                echo=echo,
                **kwargs,
            )

            # Explicitly enforce foreign key constraints on SQLite connections
            @event.listens_for(self.engine, "connect")
            def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        else:
            # Production MySQL configuration with connection pooling and pre-ping
            self.engine = create_engine(
                database_uri,
                pool_pre_ping=True,
                pool_size=10,
                max_overflow=20,
                pool_recycle=3600,
                echo=echo,
            )

        self._session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
        self.scoped_session = scoped_session(self._session_factory)

    def create_all_tables(self) -> None:
        """Create all tables registered with declarative Base."""
        Base.metadata.create_all(bind=self.engine)

    def drop_all_tables(self) -> None:
        """Drop all registered tables (used for test teardown)."""
        Base.metadata.drop_all(bind=self.engine)

    def get_session(self) -> Session:
        """Obtain a new standalone database session."""
        return self._session_factory()

    @contextmanager
    def session_scope(self) -> Generator[Session, None, None]:
        """Provide a transactional scope around a series of database operations.
        
        Guarantees automatic commit on success and rollback on any exception.
        """
        session = self.scoped_session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            self.scoped_session.remove()


# Global module singleton for the application lifecycle
_db_manager: DatabaseManager = None


def reset_db_manager() -> None:
    """Reset the global DatabaseManager singleton (primarily for testing)."""
    global _db_manager
    _db_manager = None


def get_db_manager(database_uri: str = None) -> DatabaseManager:
    """Retrieve or initialize the global DatabaseManager instance.
    
    Resolves URI first, then strictly forbids SQLite or missing configuration in production.
    """
    global _db_manager
    env = os.environ.get("FLASK_ENV", os.environ.get("ENV", "development")).lower()
    resolved_uri = database_uri or os.environ.get("DATABASE_URL")

    if not resolved_uri:
        if env == "production":
            raise ValueError(
                "DATABASE_URL must be explicitly configured in production environment. "
                "Silent fallback to SQLite is prohibited in production."
            )
        resolved_uri = "sqlite:///clarity_dev.db"

    if env == "production" and resolved_uri.startswith("sqlite"):
        raise ValueError(
            "SQLite database URI cannot be used in production environment. "
            "A production database (e.g., MySQL) URL must be configured."
        )

    if _db_manager is None or (database_uri is not None and _db_manager.database_uri != resolved_uri):
        _db_manager = DatabaseManager(resolved_uri)
    return _db_manager


def init_db(database_uri: str = None) -> DatabaseManager:
    """Explicitly initialize database tables and session factory."""
    manager = get_db_manager(database_uri)
    manager.create_all_tables()
    return manager
