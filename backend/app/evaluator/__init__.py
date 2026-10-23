"""Evaluator package initialization."""

from .benchmark_evaluator import BenchmarkEvaluator
from .benchmark_cases import (
    PolynomialGroundTruthRule,
    BenchmarkCaseDefinition,
    BENCHMARK_CASES,
    CONSTANT_BENCHMARK,
    LINEAR_BENCHMARK,
    QUADRATIC_BENCHMARK,
    CUBIC_BENCHMARK,
    NOISY_POLYNOMIAL_BENCHMARK,
    BOUNDARY_EDGE_CASES_BENCHMARK,
    get_benchmark_case,
    list_benchmark_cases,
    list_benchmark_scenarios,
)
from .benchmark_runner import (
    ActiveRandomBenchmarkRunner,
    BenchmarkComparisonSummary,
    BenchmarkTrialResult,
    StrategyAggregateStats,
)

__all__ = [
    "BenchmarkEvaluator",
    "PolynomialGroundTruthRule",
    "BenchmarkCaseDefinition",
    "BENCHMARK_CASES",
    "CONSTANT_BENCHMARK",
    "LINEAR_BENCHMARK",
    "QUADRATIC_BENCHMARK",
    "CUBIC_BENCHMARK",
    "NOISY_POLYNOMIAL_BENCHMARK",
    "BOUNDARY_EDGE_CASES_BENCHMARK",
    "get_benchmark_case",
    "list_benchmark_cases",
    "list_benchmark_scenarios",
    "ActiveRandomBenchmarkRunner",
    "BenchmarkTrialResult",
    "StrategyAggregateStats",
    "BenchmarkComparisonSummary",
]
