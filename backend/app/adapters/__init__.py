"""External-system adapters for CLARITY.

Adapters hide provider and simulation details behind the contracts defined in
``app.domain.contracts``. The ``oracle`` subpackage is the only adapter that
knows the hidden rule and must never expose it to discovery-facing objects.
"""
