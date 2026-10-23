"""Isolated Scientific Benchmark Cases for CLARITY.

STRICT TRUST BOUNDARY:
This module defines hidden reference ground-truth rules, secret benchmark
parameters, and oracle cases. These rules are reserved strictly for the
evaluator and offline scientific benchmarks. They MUST NEVER be imported,
referenced, or exposed by discovery agents, candidate generators, or
general API serializers.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..domain.contracts import GroundTruthRule, HypothesisRepresentation


class PolynomialGroundTruthRule(GroundTruthRule):
    """Ground-truth rule for 1D polynomial systems: y = sum(c_i * x^i)."""

    def evaluate_exact(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """Compute exact ground truth output for test inputs."""
        x = float(values.get("x", 0.0))
        coeffs = self._extract_coefficients(self.target_parameters)
        y = 0.0
        for power, coef in coeffs.items():
            y += coef * (x ** power)
        return {"y": y}

    @staticmethod
    def _extract_coefficients(params: Mapping[str, Any]) -> Dict[int, float]:
        """Normalize various coefficient representations into {power: float}."""
        res: Dict[int, float] = {}
        # Case 1: nested "coefficients" key
        if "coefficients" in params:
            raw = params["coefficients"]
            if isinstance(raw, (list, tuple)):
                for idx, c in enumerate(raw):
                    res[idx] = float(c)
            elif isinstance(raw, Mapping):
                for k, v in raw.items():
                    power = int(k.replace("c", "")) if isinstance(k, str) and k.startswith("c") else int(k)
                    res[power] = float(v)
        else:
            # Case 2: flat keys like "c0", "c1", etc.
            for k, v in params.items():
                if isinstance(k, str) and k.startswith("c") and k[1:].isdigit():
                    res[int(k[1:])] = float(v)
        return res

    def matches(
        self,
        candidate: HypothesisRepresentation,
        tolerance: float = 0.05,
    ) -> bool:
        """Check whether a candidate hypothesis scientifically matches this ground truth."""
        # 1. Formula exact match (ignoring whitespace)
        cand_formula = (candidate.formula or "").replace(" ", "").lower()
        tgt_formula = (self.target_formula or "").replace(" ", "").lower()
        if cand_formula and tgt_formula and cand_formula == tgt_formula:
            return True

        # 2. Check model family compatibility
        cand_family = candidate.model_family.lower() if candidate.model_family else ""
        if self.domain.lower() not in cand_family and cand_family not in self.domain.lower():
            return False

        # 3. Check degree match if both specify degree
        target_degree = self.target_parameters.get("degree")
        cand_params = candidate.parameters or {}
        cand_degree = cand_params.get("degree")
        if target_degree is not None and cand_degree is not None:
            if int(target_degree) != int(cand_degree):
                return False

        # 4. Coefficient comparison
        target_coeffs = self._extract_coefficients(self.target_parameters)
        cand_coeffs = self._extract_coefficients(cand_params)
        if target_coeffs and cand_coeffs:
            all_powers = set(target_coeffs.keys()) | set(cand_coeffs.keys())
            matched = True
            for power in all_powers:
                tc = target_coeffs.get(power, 0.0)
                cc = cand_coeffs.get(power, 0.0)
                if abs(tc - cc) > tolerance:
                    matched = False
                    break
            if matched:
                return True

        # 5. Numerical equivalence probe over diverse reference points
        probe_x_values = [-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0]
        if cand_coeffs or target_coeffs:
            all_probes_match = True
            for x_val in probe_x_values:
                exact_y = self.evaluate_exact({"x": x_val})["y"]
                cand_y = 0.0
                for p, c in cand_coeffs.items():
                    cand_y += c * (x_val ** p)
                if abs(exact_y - cand_y) > tolerance:
                    all_probes_match = False
                    break
            if all_probes_match:
                return True

        return False


@dataclass(frozen=True)
class BenchmarkCaseDefinition:
    """Preconfigured benchmark case with reference ground truth and baseline expectations.

    ``expected_baseline_queries`` must be explicitly set to a dict mapping baseline
    type names to independently measured query counts. When absent (None), the
    benchmark service will report efficiency_gain as None rather than inventing a
    comparison from an undocumented default.
    """
    benchmark_name: str
    description: str
    ground_truth: GroundTruthRule
    expected_baseline_queries: Optional[Dict[str, int]] = None


# Standard benchmark suite for CLARITY discovery
CONSTANT_BENCHMARK = BenchmarkCaseDefinition(
    benchmark_name="polynomial_constant_benchmark",
    description="Discovery of constant function y = 5.0",
    ground_truth=PolynomialGroundTruthRule(
        domain="polynomial",
        target_formula="y = 5.0",
        target_parameters={"degree": 0, "coefficients": {"c0": 5.0}},
        evaluator_label="target_constant_c5",
        secret_seed=42,
    ),
    expected_baseline_queries={"random_sampling": 15, "passive_uniform": 10},
)

LINEAR_BENCHMARK = BenchmarkCaseDefinition(
    benchmark_name="polynomial_linear_benchmark",
    description="Discovery of linear function y = 1.0 + 2.0*x",
    ground_truth=PolynomialGroundTruthRule(
        domain="polynomial",
        target_formula="y = 1.0 + 2.0*x",
        target_parameters={"degree": 1, "coefficients": {"c0": 1.0, "c1": 2.0}},
        evaluator_label="target_linear_2x_plus_1",
        secret_seed=1337,
    ),
    expected_baseline_queries={"random_sampling": 30, "passive_uniform": 25},
)

QUADRATIC_BENCHMARK = BenchmarkCaseDefinition(
    benchmark_name="polynomial_quadratic_benchmark",
    description="Discovery of quadratic curve y = -1.0 + 0.5*x^2",
    ground_truth=PolynomialGroundTruthRule(
        domain="polynomial",
        target_formula="y = -1.0 + 0.5*x^2",
        target_parameters={"degree": 2, "coefficients": {"c0": -1.0, "c1": 0.0, "c2": 0.5}},
        evaluator_label="target_quadratic_neg1_plus_half_x2",
        secret_seed=9999,
    ),
    expected_baseline_queries={"random_sampling": 60, "passive_uniform": 45},
)

CUBIC_BENCHMARK = BenchmarkCaseDefinition(
    benchmark_name="polynomial_cubic_benchmark",
    description="Discovery of cubic polynomial y = 3.0*x - 0.5*x^3",
    ground_truth=PolynomialGroundTruthRule(
        domain="polynomial",
        target_formula="y = 3.0*x - 0.5*x^3",
        target_parameters={"degree": 3, "coefficients": {"c0": 0.0, "c1": 3.0, "c2": 0.0, "c3": -0.5}},
        evaluator_label="target_cubic_3x_minus_half_x3",
        secret_seed=2024,
    ),
    expected_baseline_queries={"random_sampling": 100, "passive_uniform": 75},
)

BENCHMARK_CASES: Dict[str, BenchmarkCaseDefinition] = {
    "polynomial_constant": CONSTANT_BENCHMARK,
    "polynomial_constant_benchmark": CONSTANT_BENCHMARK,
    "target_constant_c5": CONSTANT_BENCHMARK,
    "polynomial_linear": LINEAR_BENCHMARK,
    "polynomial_linear_benchmark": LINEAR_BENCHMARK,
    "target_linear_2x_plus_1": LINEAR_BENCHMARK,
    "polynomial_quadratic": QUADRATIC_BENCHMARK,
    "polynomial_quadratic_benchmark": QUADRATIC_BENCHMARK,
    "target_quadratic_neg1_plus_half_x2": QUADRATIC_BENCHMARK,
    "polynomial_cubic": CUBIC_BENCHMARK,
    "polynomial_cubic_benchmark": CUBIC_BENCHMARK,
    "target_cubic_3x_minus_half_x3": CUBIC_BENCHMARK,
}


def get_benchmark_case(key: str) -> Optional[BenchmarkCaseDefinition]:
    """Retrieve a benchmark case definition by name, label, or alias."""
    return BENCHMARK_CASES.get(key.strip())


def list_benchmark_cases() -> List[str]:
    """List unique registered benchmark case names."""
    seen = set()
    cases = []
    for case in [CONSTANT_BENCHMARK, LINEAR_BENCHMARK, QUADRATIC_BENCHMARK, CUBIC_BENCHMARK]:
        if case.benchmark_name not in seen:
            seen.add(case.benchmark_name)
            cases.append(case.benchmark_name)
    return cases
