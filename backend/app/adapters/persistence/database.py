"""SQLAlchemy engine, session management, and Base declarative model for CLARITY.

Provides clean engine creation, scoped session factory, and transaction context managers.
Supports MySQL with PyMySQL in production/development, and SQLite for fast, isolated tests.
"""

import os
from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session, Session

# Declarative Base for all persistent models
Base = declarative_base()


class DatabaseManager:
    """Manages SQLAlchemy engine lifecycle and session factories."""

    def __init__(self, database_uri: str, echo: bool = False):
        self.database_uri = database_uri
        self.echo = echo

        # Configure connection pool settings appropriately for SQLite vs MySQL
        connect_args = {}
        if database_uri.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            self.engine = create_engine(
                database_uri,
                connect_args=connect_args,
                echo=echo,
            )
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


def get_db_manager(database_uri: str = None) -> DatabaseManager:
    """Retrieve or initialize the global DatabaseManager instance."""
    global _db_manager
    if _db_manager is None:
        if database_uri is None:
            database_uri = os.environ.get("DATABASE_URL", "sqlite:///clarity_dev.db")
        _db_manager = DatabaseManager(database_uri)
    return _db_manager


def init_db(database_uri: str = None) -> DatabaseManager:
    """Explicitly initialize database tables and session factory."""
    manager = get_db_manager(database_uri)
    manager.create_all_tables()
    return manager
