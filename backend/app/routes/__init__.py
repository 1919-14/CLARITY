"""Routes package initialization."""

from .health import health_bp
from .runs import runs_bp

__all__ = ["health_bp", "runs_bp"]
