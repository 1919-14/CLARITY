"""Application services that coordinate existing domain and adapter components.

The package currently hosts the Issue #13 parallel evaluation orchestration and
its serializable task contract. Services here orchestrate; they do not implement
scientific algorithms (see ``app.domain``) or provider SDK details (see
``app.adapters``).
"""
