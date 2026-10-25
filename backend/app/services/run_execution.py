"""Compose a persisted discovery run into an authoritative-budget execution.

Issue #14 makes the persisted per-run budget the admission authority. A budget
row that is merely stored and exposed is not enough: the normal execution path
must actually attach the persisted manager, or it must fail closed. This module
is the single composition point that does that.

Guarantees:

* The budget manager is resolved from the persisted run, never supplied by the
  caller, so a caller cannot skip it.
* An engine factory receives the resolved manager and cannot return an engine
  built without it: the composition verifies the identity of the attached port.
* A run whose budget cannot be resolved raises ``RunBudgetUnavailable`` instead
  of executing unbudgeted work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from sqlalchemy.orm import Session

from ..adapters.persistence.models import RunModel
from ..adapters.persistence.repository import ClarityRepository
from ..domain.budget_contracts import RunBudgetConfig
from ..domain.oracle_contracts import KIND_LABELS
from ..domain.run_orchestrator import RunOrchestrator
from .budget_service import RunBudgetManager, resolve_budget_config


class RunBudgetUnavailable(RuntimeError):
    """Raised when a run must not execute because its budget is unavailable."""

    def __init__(self, run_id: str, message: Optional[str] = None):
        self.run_id = run_id
        self.message = message or (
            f"Run '{run_id}' has no resolvable persisted budget; refusing to execute "
            "unbudgeted work."
        )
        super().__init__(self.message)


class RunOracleUnavailable(RuntimeError):
    """Raised when a run is bound to an artifact that can no longer be resolved."""

    def __init__(self, oracle_id: str, message: Optional[str] = None):
        self.oracle_id = oracle_id
        self.message = message or (
            f"Run is bound to uploaded oracle '{oracle_id}', which cannot be resolved. "
            "Refusing to substitute the built-in simulator."
        )
        super().__init__(self.message)


@dataclass(frozen=True)
class RunExecutionPlan:
    """Everything an engine factory is allowed to know about a run."""

    run_id: str
    domain: str
    execution_mode: str
    budget: RunBudgetConfig
    budget_manager: RunBudgetManager
    # Declared contract of the attached uploaded black box, or None for the
    # built-in local simulator. Contains the resolved artifact path on disk.
    oracle: Optional[Dict[str, Any]] = field(default=None)


# A factory receives the already-budgeted plan and must build its engine with
# ``plan.budget_manager``. It may not widen or replace the budget.
EngineFactory = Callable[[RunExecutionPlan], Any]


def resolve_run_budget(session: Session, run_id: str) -> tuple[RunModel, RunBudgetConfig]:
    """Return the persisted run and its validated budget, or fail closed."""
    repo = ClarityRepository(session)
    run = repo.get_run(run_id)
    if run is None:
        raise RunBudgetUnavailable(run_id, f"Run '{run_id}' not found.")
    try:
        config = resolve_budget_config(run)
    except Exception as exc:  # noqa: BLE001 - any failure here must fail closed
        raise RunBudgetUnavailable(
            run_id,
            f"Run '{run_id}' has an unreadable persisted budget ({type(exc).__name__}).",
        ) from None
    if not isinstance(config, RunBudgetConfig):
        raise RunBudgetUnavailable(run_id)
    return run, config


def _load_oracle_contract(
    session: Session, oracle_id: str, storage_dir: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Resolve an uploaded artifact into the contract an engine factory needs."""
    from .oracle_service import default_storage_dir, spec_from_model

    repo = ClarityRepository(session)
    artifact = repo.get_oracle_artifact(oracle_id)
    if artifact is None:
        # The run references an artifact that no longer exists; refuse rather
        # than silently falling back to the built-in simulator.
        raise RunOracleUnavailable(oracle_id)
    try:
        spec = spec_from_model(artifact, storage_dir or default_storage_dir())
    except Exception as exc:  # noqa: BLE001 - a broken binding must fail closed
        raise RunOracleUnavailable(
            oracle_id,
            f"Uploaded oracle '{oracle_id}' could not be composed for execution "
            f"({type(exc).__name__}).",
        ) from exc
    return {
        "oracle_id": artifact.oracle_id,
        "name": artifact.name,
        "kind": spec.kind.value,
        "kind_label": KIND_LABELS.get(spec.kind, spec.kind.value),
        "entrypoint": spec.entrypoint,
        "input_name": spec.input_name,
        "output_name": spec.output_name,
        "min_value": float(spec.min_value),
        "max_value": float(spec.max_value),
        "file_path": spec.file_path,
    }


def build_execution_plan(
    run_id: str,
    db_manager: Any,
    *,
    oracle_storage_dir: Optional[str] = None,
) -> RunExecutionPlan:
    """Resolve the authoritative budget and bind a manager to it.

    ``db_manager`` is a :class:`DatabaseManager`; its ``session_scope`` keeps
    every budget operation in a short transaction, so no transaction is held
    across oracle or provider work.
    """
    session_scope = db_manager.session_scope
    with session_scope() as session:
        run, config = resolve_run_budget(session, run_id)
        oracle_contract = None
        if getattr(run, "oracle_id", None):
            oracle_contract = _load_oracle_contract(
                session, run.oracle_id, oracle_storage_dir
            )
        plan = RunExecutionPlan(
            run_id=run.run_id,
            domain=run.domain,
            execution_mode=run.execution_mode,
            budget=config,
            budget_manager=RunBudgetManager(session_scope, run.run_id),
            oracle=oracle_contract,
        )
    return plan


def build_budgeted_engine(plan: RunExecutionPlan, engine_factory: EngineFactory) -> Any:
    """Build the engine through *engine_factory* and verify it carries the budget."""
    engine = engine_factory(plan)
    attached = getattr(engine, "budget", None)
    if attached is not plan.budget_manager:
        raise RunBudgetUnavailable(
            plan.run_id,
            "The engine factory returned an engine that is not bound to the "
            "authoritative per-run budget manager.",
        )
    return engine


def execute_persisted_run(
    run_id: str,
    db_manager: Any,
    engine_factory: EngineFactory,
    *,
    orchestrator: Optional[RunOrchestrator] = None,
    initial_observations: Any = (),
    oracle_storage_dir: Optional[str] = None,
) -> Any:
    """Execute a persisted run with the authoritative budget attached.

    Raises :class:`RunBudgetUnavailable` before any oracle or provider call when
    the run cannot be composed with its persisted budget, and
    :class:`RunOracleUnavailable` when its uploaded artifact is missing.
    """
    plan = build_execution_plan(
        run_id, db_manager, oracle_storage_dir=oracle_storage_dir
    )
    engine = build_budgeted_engine(plan, engine_factory)
    runner = orchestrator or RunOrchestrator()
    return runner.execute_run(
        run_id,
        engine,
        initial_observations=initial_observations,
        session_factory=db_manager.get_session,
    )


__all__ = [
    "RunBudgetUnavailable",
    "RunOracleUnavailable",
    "RunExecutionPlan",
    "EngineFactory",
    "resolve_run_budget",
    "build_execution_plan",
    "build_budgeted_engine",
    "execute_persisted_run",
]
