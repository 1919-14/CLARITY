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

from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from ..adapters.persistence.models import RunModel
from ..adapters.persistence.repository import ClarityRepository
from ..domain.budget_contracts import RunBudgetConfig
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


@dataclass(frozen=True)
class RunExecutionPlan:
    """Everything an engine factory is allowed to know about a run."""

    run_id: str
    domain: str
    execution_mode: str
    budget: RunBudgetConfig
    budget_manager: RunBudgetManager


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


def build_execution_plan(run_id: str, db_manager: Any) -> RunExecutionPlan:
    """Resolve the authoritative budget and bind a manager to it.

    ``db_manager`` is a :class:`DatabaseManager`; its ``session_scope`` keeps
    every budget operation in a short transaction, so no transaction is held
    across oracle or provider work.
    """
    session_scope = db_manager.session_scope
    with session_scope() as session:
        run, config = resolve_run_budget(session, run_id)
        plan = RunExecutionPlan(
            run_id=run.run_id,
            domain=run.domain,
            execution_mode=run.execution_mode,
            budget=config,
            budget_manager=RunBudgetManager(session_scope, run.run_id),
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
) -> Any:
    """Execute a persisted run with the authoritative budget attached.

    Raises :class:`RunBudgetUnavailable` before any oracle or provider call when
    the run cannot be composed with its persisted budget.
    """
    plan = build_execution_plan(run_id, db_manager)
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
    "RunExecutionPlan",
    "EngineFactory",
    "resolve_run_budget",
    "build_execution_plan",
    "build_budgeted_engine",
    "execute_persisted_run",
]
