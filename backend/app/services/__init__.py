"""Services layer for CLARITY.

Coordinates domain models, persistence, and business logic across API routes
and background processes. The package also hosts the Issue #13 parallel
evaluation orchestration and its serializable task contract.

Services here orchestrate; they do not implement scientific algorithms (see
``app.domain``) or provider SDK details (see ``app.adapters``).
"""
