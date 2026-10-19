import os
import sys
import pytest

# Ensure backend root is on sys.path for direct imports
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app import create_app


@pytest.fixture
def app():
    """Create and configure a Flask application for testing."""
    test_app = create_app("testing")
    yield test_app


@pytest.fixture
def client(app):
    """A test client for the application."""
    return app.test_client()
