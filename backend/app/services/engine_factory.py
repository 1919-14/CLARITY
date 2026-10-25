"""Default discovery-engine factory used by the run start endpoint.

The factory receives a composition-verified :class:`RunExecutionPlan` whose
budget manager must be attached to the engine it returns.

Two oracle sources are possible:

* no bound artifact → the deterministic built-in polynomial simulator, with the
  historical fixed search domain;
* a bound artifact → an :class:`UploadedOracle` using the artifact's declared
  bounds, input name and entrypoint. The candidate search widens to degree 3 so
  a black box that is not quadratic can still be distilled, and every ranking
  claim remains the ranking engine's, not a claimed ground truth.
"""

from __future__ import annotations

from typing import Any

from ..adapters.llm import build_llm_scientist_from_environment
from ..adapters.oracle.local_simulator import PolynomialLocalOracle
from ..adapters.oracle.uploaded import UploadedOracle
from ..domain.candidate_generator import HypothesisGenerationConfig
from ..domain.discovery_contracts import DiscoveryRunConfig
from ..domain.discovery_engine import DiscoveryEngine
from ..domain.experiment_generator import NumericalDomainBounds
from ..domain.oracle_contracts import OracleKind, OracleSpec
from ..domain.ranking_contracts import RankingPolicyConfig
from .run_execution import RunExecutionPlan, RunOracleUnavailable

# Local execution does not enforce the engine-local query counter as a budget;
# the persisted budget manager remains the admission authority.
_LOCAL_UNLIMITED = 100_000


def _build_config(
    *,
    plan: RunExecutionPlan,
    bounds: NumericalDomainBounds,
    hypothesis_config: HypothesisGenerationConfig,
) -> DiscoveryRunConfig:
    return DiscoveryRunConfig(
        max_query_budget=_LOCAL_UNLIMITED,
        max_iterations=plan.budget.max_oracle_queries or 10,
        domain_bounds=bounds,
        hypothesis_config=hypothesis_config,
        ranking_policy=RankingPolicyConfig(
            min_samples_for_plausibility=2, performance_acceptance_threshold=1e9
        ),
        experiment_pool_size=4,
    )


def default_engine_factory(plan: RunExecutionPlan) -> Any:
    """Build the discovery engine for one persisted run."""
    scientist = build_llm_scientist_from_environment()

    if not plan.oracle:
        oracle = PolynomialLocalOracle()
        config = _build_config(
            plan=plan,
            bounds=NumericalDomainBounds(
                variable_name="x",
                min_val=-2.0,
                max_val=2.0,
                domain_name="1d_polynomial",
            ),
            hypothesis_config=HypothesisGenerationConfig(
                min_degree=0, max_degree=2, candidate_count=4, seed=42
            ),
        )
        return DiscoveryEngine(
            oracle,
            config,
            run_id=plan.run_id,
            budget=plan.budget_manager,
            scientist=scientist,
        )

    contract = plan.oracle
    try:
        spec = OracleSpec(
            kind=OracleKind(contract["kind"]),
            file_path=contract["file_path"],
            entrypoint=contract.get("entrypoint"),
            input_name=contract["input_name"],
            output_name=contract["output_name"],
            min_value=float(contract["min_value"]),
            max_value=float(contract["max_value"]),
        )
        oracle = UploadedOracle(spec)
    except Exception as exc:  # noqa: BLE001 - fail closed, never substitute silently
        raise RunOracleUnavailable(
            str(contract.get("oracle_id") or "unknown"),
            f"Uploaded oracle could not be loaded for execution "
            f"({exc.__class__.__name__}: {exc}).",
        ) from exc

    config = _build_config(
        plan=plan,
        bounds=NumericalDomainBounds(
            variable_name=spec.input_name,
            min_val=float(spec.min_value),
            max_val=float(spec.max_value),
            domain_name=plan.domain,
        ),
        hypothesis_config=HypothesisGenerationConfig(
            min_degree=0, max_degree=3, candidate_count=4, seed=42
        ),
    )
    return DiscoveryEngine(
        oracle,
        config,
        run_id=plan.run_id,
        budget=plan.budget_manager,
        scientist=scientist,
    )


__all__ = ["default_engine_factory"]
