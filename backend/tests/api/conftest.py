"""Conftest for API integration tests.

Creates all database tables before each test and tears them down afterwards
so that every test runs with a clean, isolated in-memory SQLite database.
Also resets the global DatabaseManager singleton and lifecycle manager
so each test gets fully isolated state.
"""

import pytest
from app.adapters.persistence.database import get_db_manager, reset_db_manager
from app.adapters.persistence import models  # ensure all ORM models are imported
from app.domain.run_lifecycle import RunLifecycleManager


@pytest.fixture(autouse=True)
def setup_test_database(app):
    """Create all tables before each test, drop them after.
    
    Also injects a fresh RunLifecycleManager into the app config so that
    in-memory run state does not bleed between tests.
    """
    with app.app_context():
        db_uri = app.config["SQLALCHEMY_DATABASE_URI"]
        reset_db_manager()
        db = get_db_manager(db_uri)
        db.create_all_tables()
        # Fresh lifecycle manager per test — no shared in-memory state
        app.config["LIFECYCLE_MANAGER"] = RunLifecycleManager()
        yield
        db.drop_all_tables()
        reset_db_manager()
