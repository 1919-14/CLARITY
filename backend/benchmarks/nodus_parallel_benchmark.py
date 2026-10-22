"""Sequential-versus-parallel evaluation benchmark (Issue #13).

Usage
-----
::

    # Offline: correctness and local timing of the shared workload.
    python -m benchmarks.nodus_parallel_benchmark

    # Opt-in: also run the same workload on the real Nodus platform.
    python -m benchmarks.nodus_parallel_benchmark --remote

The remote run is **not** part of CI and consumes credits. It is not executed by
this repository's automated checks, and no remote numbers are recorded here:
live benchmarking is pending until credentials and credits are available.

What is measured
----------------
The workload is a deterministic set of independent candidate evaluations over
the same permitted observations (polynomial family, fixed seeds). Both modes use
the same payload contract and worker function, so result equivalence is directly
comparable.

Reported values are measurements from this process only:

* wall-clock duration of each mode,
* per-task outcome counts,
* configured concurrency,
* requested and actual execution mode,
* provider task IDs and provider metadata when the adapter reports them,
* result equivalence (metrics per hypothesis),
* cost status, which stays ``unavailable`` because the verified SDK surface
  exposes no per-call charge.

A local-versus-remote wall-clock difference is **not** a provider speedup: the
two runs execute in different venues. A venue-matched parallel-versus-sequential
remote comparison requires two billable remote runs (concurrency 1 and N) and is
deliberately left to an explicit future run.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from typing import Any, Dict, List

from app.adapters.nodus import ExecutionMode, NodusConfig, NodusExecutionAdapter
from app.domain.contracts import EvaluationRequest, HypothesisRepresentation, Observation, ObservationStatus
from app.domain.hypothesis_models import POLYNOMIAL_FAMILY_ID
from app.services.parallel_evaluation import (
    ParallelEvaluationConfig,
    ParallelEvaluationRunner,
    ParallelExecutionMode,
)

WORKLOAD_CANDIDATES = 8
BENCHMARK_SEED = 20261023


def truth(x: float) -> float:
    return 3.0 - (2.0 * x) - (x**2) + (0.5 * (x**3))


def make_observations(count: int = 7) -> Dict[str, Observation]:
    observations: Dict[str, Observation] = {}
    for index in range(count):
        x = -3.0 + index
        observation_id = f"obs-{index}"
        observations[observation_id] = Observation(
            observation_id=observation_id,
            input_id=f"inp-{index}",
            input_values={"x": x},
            output_values={"y": truth(x)},
            status=ObservationStatus.SUCCESS,
            sequence_number=index,
        )
    return observations


def make_candidate(degree: int, hypothesis_id: str) -> HypothesisRepresentation:
    # Deterministic, seeded coefficient variation so the workload is not uniform.
    coefficients = [0.0] * (degree + 1)
    coefficients[degree] = 1.0
    coefficients[0] = float((BENCHMARK_SEED + degree) % 5)
    specification = {
        "family": POLYNOMIAL_FAMILY_ID,
        "degree": degree,
        "coefficient_order": "ascending",
        "coefficients": {f"c{index}": value for index, value in enumerate(coefficients)},
        "input_variable": "x",
        "output_variable": "y",
    }
    return HypothesisRepresentation(
        model_family=POLYNOMIAL_FAMILY_ID,
        formula=f"benchmark-degree-{degree}",
        parameters=specification,
        complexity=degree + 1,
        hypothesis_id=hypothesis_id,
    )


def build_requests() -> List[EvaluationRequest]:
    observations = make_observations()
    ids = sorted(observations)
    return [
        EvaluationRequest(
            hypothesis=make_candidate(index % 4, f"bench-hyp-{index}"),
            observation_ids=list(ids),
            held_out_observation_ids=[],
            request_id=f"bench-req-{index}",
            metric_names=["mae", "rmse"],
        )
        for index in range(WORKLOAD_CANDIDATES)
    ]


def _metrics_by_hypothesis(results) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {}
    for result in results:
        if result.evaluation is None:
            metrics[result.hypothesis_id] = {
                "outcome": result.outcome.value,
                "error": result.error.code if result.error else None,
            }
        else:
            metrics[result.hypothesis_id] = {
                "outcome": result.outcome.value,
                "metrics": dict(result.evaluation.metrics),
                "fitted": dict(result.evaluation.fitted_specification or {}),
                "status": result.evaluation.status.value,
            }
    return metrics


def _environment() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }
    try:
        import nodus  # noqa: PLC0415 - optional dependency, lazy by design

        info["nodus_compute"] = getattr(nodus, "__version__", "unknown")
    except ImportError:
        info["nodus_compute"] = "not installed"
    return info


def run_local(request_count: int) -> Dict[str, Any]:
    from app.services.parallel_evaluation import ParallelEvaluationRunner

    config = ParallelEvaluationConfig(
        execution_mode=ParallelExecutionMode.LOCAL_SEQUENTIAL,
        max_concurrency=1,
        max_retries=0,
        retry_backoff_seconds=0.0,
    )
    runner = ParallelEvaluationRunner(NodusExecutionAdapter(_mock_config()), config)
    batch = runner.run(build_requests()[:request_count], make_observations())
    return {
        "mode": batch.summary.actual_mode,
        "duration_seconds": batch.summary.duration_seconds,
        "completed_tasks": batch.summary.completed_tasks,
        "failed_tasks": batch.summary.failed_tasks,
        "timed_out_tasks": batch.summary.timed_out_tasks,
        "metrics": _metrics_by_hypothesis(batch.results),
        "batch": batch,
    }


def _mock_config() -> NodusConfig:
    # Local mode never dispatches, but the runner reads the adapter's timeout.
    return NodusConfig(
        execution_mode=ExecutionMode.MOCK,
        app_name="benchmark-local",
        function_name="benchmark-local",
        api_key=None,
        timeout_seconds=30.0,
        max_retries=0,
    )


def run_remote(request_count: int) -> Dict[str, Any]:
    adapter_config = NodusConfig.from_environment()
    if adapter_config.execution_mode != ExecutionMode.REMOTE:
        raise SystemExit(
            "[ABORT] --remote requires NODUS_EXECUTION_MODE=nodus_remote with "
            "NODUS_API_KEY, NODUS_APP_NAME, and NODUS_FUNCTION_NAME configured."
        )
    parallel_config = ParallelEvaluationConfig.from_environment()
    if parallel_config.execution_mode != ParallelExecutionMode.NODUS_PARALLEL:
        raise SystemExit(
            "[ABORT] --remote requires NODUS_PARALLEL_EXECUTION_MODE=nodus_parallel."
        )

    adapter = NodusExecutionAdapter(adapter_config)
    runner = ParallelEvaluationRunner(adapter, parallel_config)
    batch = runner.run(build_requests()[:request_count], make_observations(), run_id="benchmark")
    return {
        "mode": batch.summary.actual_mode,
        "requested_mode": batch.summary.requested_mode,
        "duration_seconds": batch.summary.duration_seconds,
        "configured_concurrency": batch.summary.max_concurrency,
        "completed_tasks": batch.summary.completed_tasks,
        "failed_tasks": batch.summary.failed_tasks,
        "timed_out_tasks": batch.summary.timed_out_tasks,
        "retry_attempts": batch.summary.retry_attempts,
        "provider_metadata_available": batch.summary.provider_metadata_available,
        "provider_task_ids": [
            result.provider_task_id for result in batch.results if result.provider_task_id
        ],
        "cost_status": batch.summary.cost_status,
        "metrics": _metrics_by_hypothesis(batch.results),
        "batch": batch,
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Also run the same workload on the real Nodus platform (billable, opt-in).",
    )
    parser.add_argument(
        "--tasks",
        type=int,
        default=WORKLOAD_CANDIDATES,
        help=f"Number of independent evaluations (default {WORKLOAD_CANDIDATES}).",
    )
    args = parser.parse_args(argv)
    if args.tasks < 1 or args.tasks > 100:
        parser.error("--tasks must be between 1 and 100")

    report: Dict[str, Any] = {
        "benchmark": "issue-13-sequential-vs-parallel",
        "workload": {
            "description": (
                "Independent polynomial candidate evaluations over identical "
                "permitted observations; same payload contract in both modes."
            ),
            "tasks": args.tasks,
            "candidate_count": WORKLOAD_CANDIDATES,
            "seed": BENCHMARK_SEED,
        },
        "environment": _environment(),
        "local_sequential": None,
        "nodus_parallel": None,
        "comparison": None,
        "remote_benchmark_status": "pending (requires Nodus credentials/credits)",
    }

    local = run_local(args.tasks)
    report["local_sequential"] = {
        key: value for key, value in local.items() if key != "batch"
    }

    if not args.remote:
        report["comparison"] = {
            "result_equivalence": None,
            "note": (
                "Remote benchmark not run. Local timing measures this process "
                "only and must not be presented as a Nodus speedup or saving."
            ),
        }
        print(json.dumps(report, indent=2, default=str))
        return 0

    remote = run_remote(args.tasks)
    report["nodus_parallel"] = {
        key: value for key, value in remote.items() if key != "batch"
    }
    report["comparison"] = {
        "result_equivalence": local["metrics"] == remote["metrics"],
        "local_duration_seconds": local["duration_seconds"],
        "parallel_duration_seconds": remote["duration_seconds"],
        "duration_ratio_local_over_parallel": (
            local["duration_seconds"] / remote["duration_seconds"]
            if remote["duration_seconds"]
            else None
        ),
        "caveat": (
            "The two runs executed in different venues (in-process vs Nodus "
            "remote), so the ratio is not a provider speedup. A venue-matched "
            "comparison requires two billable remote runs at concurrency 1 and N."
        ),
    }
    report["remote_benchmark_status"] = "performed for this workload, single run"
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["comparison"]["result_equivalence"] else 1


if __name__ == "__main__":
    sys.exit(main())
