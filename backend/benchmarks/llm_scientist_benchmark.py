"""Synthetic comparison: deterministic-only vs LLM-assisted hypothesis generation.

The benchmark runs the *same* discovery configuration twice per seed:

- ``deterministic``: the repository's seeded candidate generator only.
- ``llm_assisted``: identical configuration plus the optional LLM Scientist, fed
  by a fixed offline fake provider whose scripted proposals are the same for every
  seed. No network call, no credentials, and no live-provider claim is made.

Both arms share the synthetic oracle, initial observations, query budget,
experiment generator, active experiment-selection planner, fitting, evaluation,
ranking, and stopping policy. Ground-truth scoring happens only *after* each run,
in this evaluator-side script, using the oracle's evaluator-only coefficients.

Reported metrics are measured, not assumed:
- best held-out MAE over evaluated candidates (lower is better),
- proposals returned/accepted/rejected by the scientist,
- oracle queries used and the run's terminal status/reason.

Run:
    cd backend
    python -m benchmarks.llm_scientist_benchmark
    python -m benchmarks.llm_scientist_benchmark --seeds 1,2,3,4,5 --json
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from typing import Any, Dict, List, Sequence

from app.adapters.oracle.local_simulator import PolynomialLocalOracle
from app.domain.candidate_generator import HypothesisGenerationConfig
from app.domain.contracts import EvaluationStatus, FittingStatus, Observation
from app.domain.discovery_contracts import DiscoveryRunConfig
from app.domain.discovery_engine import DiscoveryEngine
from app.domain.evaluation import EvaluationPipeline, select_samples
from app.domain.experiment_generator import (
    ExperimentGenerationStrategy,
    NumericalDomainBounds,
)
from app.domain.experiment_selection import ExperimentSelectionPlanner
from app.domain.fitting import FittingConfig
from app.domain.hypothesis_models import default_registry
from app.domain.llm_scientist import LLMScientist
from app.domain.llm_scientist_contracts import (
    LLMScientistConfig,
    ProviderCallResult,
    ProviderOutcomeStatus,
)
from app.domain.ranking_contracts import RankingPolicyConfig

DOMAIN = NumericalDomainBounds(
    variable_name="x", min_val=-2.0, max_val=2.0, domain_name="benchmark_polynomial"
)

INITIAL_X = (-1.5, -0.5, 0.0, 0.5, 1.5)
HELD_OUT_X = tuple(-2.0 + index * (4.0 / 20) for index in range(21))

# Fixed offline proposals: deliberately arbitrary hand-authored polynomials, not
# the hidden rule. The same script is used for every seed so the comparison is
# reproducible and cannot be tuned per seed.
PROPOSAL_SCRIPT: Sequence[Dict[str, Any]] = (
    {"degree": 3, "coefficients": [1.0, -0.5, 0.25, 0.1], "rationale": "Fixed offline script A."},
    {"degree": 2, "coefficients": [0.5, 1.5, -0.4], "rationale": "Fixed offline script B."},
    {"degree": 1, "coefficients": [-1.0, 2.0], "rationale": "Fixed offline script C."},
    {"degree": 3, "coefficients": [2.0, -1.0, 0.5, -0.2], "rationale": "Fixed offline script D."},
)


class ScriptedFakeProvider:
    """Offline provider double: fixed proposals, explicitly not a live call."""

    provider_id = "benchmark_fake_provider"
    model_id = "fixed-script-v1"
    is_live = False

    def __init__(self, script: Sequence[Dict[str, Any]]) -> None:
        self._script = list(script)
        self.calls = 0

    def propose(self, request):
        self.calls += 1
        # Rotate one proposal per call so successive rounds see a new hypothesis.
        item = self._script[(self.calls - 1) % len(self._script)]
        payload = {
            "hypotheses": [
                {
                    "model_family": "polynomial",
                    "degree": item["degree"],
                    "coefficients": list(item["coefficients"]),
                    "rationale": item["rationale"],
                    "assumptions": ["Synthetic benchmark script; confidence is uncalibrated."],
                    "competing_explanations": ["A lower-degree model may fit equally well."],
                    "distinguishing_evidence": ["Held-out curvature."],
                    "suggested_experiment_inputs": [],
                }
            ]
        }
        return ProviderCallResult(
            status=ProviderOutcomeStatus.SUCCESS,
            content=json.dumps(payload),
            attempts=1,
            model=self.model_id,
            is_live=False,
        )


def build_config(seed: int) -> DiscoveryRunConfig:
    return DiscoveryRunConfig(
        max_query_budget=6,
        max_iterations=8,
        domain_bounds=DOMAIN,
        hypothesis_config=HypothesisGenerationConfig(
            min_degree=0, max_degree=3, candidate_count=6, seed=seed
        ),
        fitting_config=FittingConfig(),
        # Synthetic noiseless benchmark policy: wide residual tolerances keep
        # several candidates in competing (plausible) status so active selection
        # can measure disagreement. This is a benchmark setting, not a claim that
        # such tolerances are appropriate for real data.
        ranking_policy=RankingPolicyConfig(
            min_samples_for_plausibility=3,
            performance_acceptance_threshold=100.0,
            tolerance_absolute=100.0,
            tolerance_relative=0.0,
            max_tolerated_contradictions=10,
        ),
        experiment_pool_size=8,
        experiment_strategy=ExperimentGenerationStrategy.STRATIFIED_RANDOM,
        experiment_seed=seed,
    )


def make_initial_observations() -> List[Observation]:
    oracle = PolynomialLocalOracle(min_x=DOMAIN.min_val, max_x=DOMAIN.max_val)
    observations: List[Observation] = []
    for index, x in enumerate(INITIAL_X, start=1):
        observations.append(
            Observation(
                observation_id=f"bench-init-{index}",
                input_id=f"bench-input-{index}",
                input_values={"x": float(x)},
                output_values={"y": float(ground_truth_value(oracle, float(x)))},
                sequence_number=index,
            )
        )
    return observations


def ground_truth_value(oracle: PolynomialLocalOracle, x: float) -> float:
    """Evaluator-side truth for benchmark scoring only (never passed to discovery)."""
    truth = oracle._evaluator_get_ground_truth()
    coefficients = truth["coefficients"]
    return (
        coefficients["w0"]
        + coefficients["w1"] * x
        + coefficients["w2"] * x**2
        + coefficients["w3"] * x**3
    )


def held_out_mae(oracle: PolynomialLocalOracle, result) -> float | None:
    """Best held-out MAE over candidates with a completed evaluation."""
    registry = default_registry(max_degree=3)
    family = registry.get("polynomial")
    assert family is not None
    truth = [ground_truth_value(oracle, x) for x in HELD_OUT_X]

    best: float | None = None
    for evaluation in result.evaluations:
        if evaluation.status != EvaluationStatus.COMPLETED:
            continue
        if (
            evaluation.fitting_status != FittingStatus.SUCCESS
            or evaluation.fitted_specification is None
        ):
            continue
        errors: List[float] = []
        for x, expected in zip(HELD_OUT_X, truth):
            prediction, error = family.predict(evaluation.fitted_specification, {"x": x})
            if error is not None or prediction is None:
                errors = []
                break
            errors.append(abs(float(prediction["y"]) - expected))
        if not errors:
            continue
        mae = sum(errors) / len(errors)
        if math.isfinite(mae) and (best is None or mae < best):
            best = mae
    return best


def run_arm(seed: int, *, llm_assisted: bool) -> Dict[str, Any]:
    oracle = PolynomialLocalOracle(min_x=DOMAIN.min_val, max_x=DOMAIN.max_val)
    scientist = None
    provider = None
    if llm_assisted:
        provider = ScriptedFakeProvider(PROPOSAL_SCRIPT)
        scientist = LLMScientist(
            LLMScientistConfig(
                enabled=True,
                model=provider.model_id,
                max_proposals_per_round=1,
                max_rounds=3,
                max_retries=0,
            ),
            provider,
        )
    engine = DiscoveryEngine(
        oracle,
        build_config(seed),
        scientist=scientist,
        planner=ExperimentSelectionPlanner(),
        run_id=f"bench-{seed}-{'llm' if llm_assisted else 'det'}",
    )
    result = engine.run(make_initial_observations())

    proposals_returned = sum(record.proposals_returned for record in result.scientist_history)
    accepted = sum(record.accepted_count for record in result.scientist_history)
    rejected = sum(record.rejected_count for record in result.scientist_history)
    fallback_rounds = sum(1 for record in result.scientist_history if record.fallback_applied)

    return {
        "seed": seed,
        "arm": "llm_assisted" if llm_assisted else "deterministic",
        "best_held_out_mae": held_out_mae(oracle, result),
        "candidate_count": len(result.hypotheses),
        "proposals_returned": proposals_returned,
        "proposals_accepted": accepted,
        "proposals_rejected": rejected,
        "fallback_rounds": fallback_rounds,
        "provider_calls": provider.calls if provider is not None else 0,
        "queries_used": result.query_accounting.attempted_queries,
        "status": result.status.value,
        "termination_reason": (
            result.termination_reason.value if result.termination_reason is not None else None
        ),
    }


def _aggregate(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    errors = [row["best_held_out_mae"] for row in rows if row["best_held_out_mae"] is not None]
    return {
        "runs": len(rows),
        "mean_best_held_out_mae": statistics.fmean(errors) if errors else None,
        "median_best_held_out_mae": statistics.median(errors) if errors else None,
        "mean_queries_used": statistics.fmean([row["queries_used"] for row in rows]),
        "total_proposals_returned": sum(row["proposals_returned"] for row in rows),
        "total_proposals_accepted": sum(row["proposals_accepted"] for row in rows),
        "total_proposals_rejected": sum(row["proposals_rejected"] for row in rows),
        "fallback_rounds": sum(row["fallback_rounds"] for row in rows),
    }


def run_benchmark(seeds: Sequence[int]) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for seed in seeds:
        rows.append(run_arm(seed, llm_assisted=False))
        rows.append(run_arm(seed, llm_assisted=True))

    deterministic = [row for row in rows if row["arm"] == "deterministic"]
    assisted = [row for row in rows if row["arm"] == "llm_assisted"]
    return {
        "benchmark": "llm_scientist_deterministic_vs_offline_fake_llm",
        "provider_is_live": False,
        "provider_note": (
            "Proposals come from a fixed in-process fake provider. This is not "
            "evidence that a live Groq call works."
        ),
        "seeds": list(seeds),
        "per_seed": rows,
        "aggregate": {
            "deterministic": _aggregate(deterministic),
            "llm_assisted": _aggregate(assisted),
        },
        "limitations": [
            "One synthetic noiseless cubic oracle, one small domain, and one shared "
            "budget/planner. Both arms contain a degree-3 candidate, so both can fit it "
            "exactly; the benchmark exercises the integration path rather than proving a "
            "performance advantage.",
            "LLM proposals are a fixed hand-authored script and are not optimized; the "
            "scripted arm measures the plumbing, not model quality.",
            "Candidate fitting and ranking use all committed observations; no held-out "
            "partition exists inside discovery. Held-out MAE is computed here only.",
            "No live provider was called, so no live latency/cost/usage data exists.",
        ],
    }


def _format_table(report: Dict[str, Any]) -> str:
    lines = [
        "Synthetic comparison: deterministic-only vs offline-fake LLM-assisted discovery",
        "Provider execution: NOT LIVE (fixed fake provider; no Groq call was made)",
        "",
        f"{'seed':>4}  {'arm':<14} {'best held-out MAE':>18} {'queries':>8} "
        f"{'accepted':>9} {'rejected':>9} {'status':<32}",
    ]
    for row in report["per_seed"]:
        mae = row["best_held_out_mae"]
        mae_text = "unavailable" if mae is None else f"{mae:.3e}"
        lines.append(
            f"{row['seed']:>4}  {row['arm']:<14} {mae_text:>18} {row['queries_used']:>8} "
            f"{row['proposals_accepted']:>9} {row['proposals_rejected']:>9} "
            f"{row['status']:<32}"
        )
    lines.append("")
    for arm, aggregate in report["aggregate"].items():
        mean = aggregate["mean_best_held_out_mae"]
        mean_text = "unavailable" if mean is None else f"{mean:.3e}"
        lines.append(
            f"{arm}: runs={aggregate['runs']} mean held-out MAE={mean_text} "
            f"mean queries={aggregate['mean_queries_used']:.2f} "
            f"proposals accepted={aggregate['total_proposals_accepted']} "
            f"rejected={aggregate['total_proposals_rejected']}"
        )
    lines.append("")
    lines.append("Limitations:")
    for item in report["limitations"]:
        lines.append(f"- {item}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seeds",
        default="1,2,3,4,5",
        help="Comma-separated integer seeds (default: 1,2,3,4,5).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full JSON report instead of the human-readable table.",
    )
    args = parser.parse_args(argv)

    try:
        seeds = [int(item) for item in args.seeds.split(",") if item.strip()]
    except ValueError:
        parser.error("--seeds must be a comma-separated list of integers.")
    if not seeds:
        parser.error("--seeds must contain at least one integer.")

    report = run_benchmark(seeds)
    print(json.dumps(report, indent=2) if args.json else _format_table(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
