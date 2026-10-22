import os
import sys
import pytest

# Ensure backend root is on sys.path for direct imports
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app import create_app
from app.adapters.persistence.database import DatabaseManager, get_db_manager, reset_db_manager


@pytest.fixture
def app():
    """Create and configure a Flask application for testing with an isolated database."""
    reset_db_manager()
    test_app = create_app("testing")
    db_uri = test_app.config.get("SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
    db_manager = get_db_manager(db_uri)
    db_manager.create_all_tables()
    yield test_app
    db_manager.drop_all_tables()
    reset_db_manager()


@pytest.fixture
def client(app):
    """A test client for the application."""
    return app.test_client()
