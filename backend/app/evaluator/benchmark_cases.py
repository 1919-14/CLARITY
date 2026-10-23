"""Isolated Scientific Benchmark Cases for CLARITY.

STRICT TRUST BOUNDARY:
This module defines hidden reference ground-truth rules, secret benchmark
parameters, and oracle cases. These rules are reserved strictly for the
evaluator and offline scientific benchmarks. They MUST NEVER be imported,
referenced, or exposed by discovery agents, candidate generators, or
general API serializers.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..domain.contracts import GroundTruthRule, HypothesisRepresentation, OracleInterface
from ..domain.experiment_generator import NumericalDomainBounds


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
    scenario_id: Optional[str] = None
    version: str = "1.0.0"
    challenge_type: str = "simple_numerical"  # simple_numerical, nonlinear, competing_hypotheses, noisy, boundary_edge_cases
    domain_bounds: NumericalDomainBounds = field(
        default_factory=lambda: NumericalDomainBounds(variable_name="x", min_val=-2.0, max_val=2.0, domain_name="1d_polynomial")
    )
    max_query_budget: int = 10
    success_threshold: float = 0.05
    noise_std: float = 0.0
    held_out_inputs: Tuple[Dict[str, float], ...] = ()
    initial_inputs: Tuple[Dict[str, float], ...] = ({"x": 0.0},)

    @property
    def id(self) -> str:
        return self.scenario_id or self.benchmark_name

    def create_oracle(self, noise_seed: Optional[int] = None) -> OracleInterface:
        """Instantiate an isolated oracle simulator for this scenario without leaking ground-truth objects."""
        from ..adapters.oracle.local_simulator import PolynomialLocalOracle
        target_params = self.ground_truth.target_parameters or {}
        raw_coeffs = PolynomialGroundTruthRule._extract_coefficients(target_params)
        coeffs = {f"w{p}": val for p, val in raw_coeffs.items()}
        return PolynomialLocalOracle(
            domain_name=self.domain_bounds.domain_name,
            min_x=self.domain_bounds.min_val,
            max_x=self.domain_bounds.max_val,
            coefficients=coeffs,
            noise_std=self.noise_std,
            seed=noise_seed,
        )

    def generate_held_out_dataset(self) -> List[Dict[str, Any]]:
        """Compute exact ground truth answers for held-out inputs (Evaluator-side only!)."""
        inputs = self.held_out_inputs
        if not inputs:
            span = self.domain_bounds.max_val - self.domain_bounds.min_val
            inputs = tuple(
                {"x": round(self.domain_bounds.min_val + i * span / 9.0, 4)}
                for i in range(1, 9)
            )
        dataset = []
        for inp in inputs:
            out = self.ground_truth.evaluate_exact(inp)
            dataset.append({
                "input": dict(inp),
                "expected_output": dict(out),
            })
        return dataset

    def to_sanitized_dict(self) -> Dict[str, Any]:
        """Expose scenario metadata for discovery/API without leaking evaluator ground-truth rules."""
        return {
            "scenario_id": self.id,
            "benchmark_name": self.benchmark_name,
            "version": self.version,
            "description": self.description,
            "challenge_type": self.challenge_type,
            "domain_bounds": {
                "variable_name": self.domain_bounds.variable_name,
                "min_val": self.domain_bounds.min_val,
                "max_val": self.domain_bounds.max_val,
                "domain_name": self.domain_bounds.domain_name,
            },
            "max_query_budget": self.max_query_budget,
            "success_threshold": self.success_threshold,
            "noise_std": self.noise_std,
            "initial_inputs": [dict(inp) for inp in self.initial_inputs],
            "held_out_sample_count": len(self.held_out_inputs) if self.held_out_inputs else 8,
        }


# Standard benchmark suite for CLARITY discovery
CONSTANT_BENCHMARK = BenchmarkCaseDefinition(
    benchmark_name="polynomial_constant_benchmark",
    scenario_id="polynomial_constant_benchmark",
    description="Discovery of constant function y = 5.0",
    challenge_type="simple_numerical",
    domain_bounds=NumericalDomainBounds("x", -5.0, 5.0, "1d_polynomial"),
    max_query_budget=8,
    success_threshold=0.05,
    noise_std=0.0,
    held_out_inputs=tuple({"x": float(x)} for x in [-4.0, -2.5, -1.0, 1.0, 2.5, 4.0]),
    initial_inputs=({"x": 0.0},),
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
    scenario_id="polynomial_linear_benchmark",
    description="Discovery of linear function y = 1.0 + 2.0*x",
    challenge_type="simple_numerical",
    domain_bounds=NumericalDomainBounds("x", -5.0, 5.0, "1d_polynomial"),
    max_query_budget=8,
    success_threshold=0.05,
    noise_std=0.0,
    held_out_inputs=tuple({"x": float(x)} for x in [-4.5, -3.0, -1.5, 1.5, 3.0, 4.5]),
    initial_inputs=({"x": 0.0},),
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
    scenario_id="polynomial_quadratic_benchmark",
    description="Discovery of quadratic curve y = -1.0 + 0.5*x^2",
    challenge_type="nonlinear",
    domain_bounds=NumericalDomainBounds("x", -4.0, 4.0, "1d_polynomial"),
    max_query_budget=10,
    success_threshold=0.05,
    noise_std=0.0,
    held_out_inputs=tuple({"x": float(x)} for x in [-3.5, -2.2, -1.1, 1.1, 2.2, 3.5]),
    initial_inputs=({"x": 0.0},),
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
    scenario_id="polynomial_cubic_benchmark",
    description="Discovery of cubic polynomial y = 3.0*x - 0.5*x^3 with competing behavior near origin",
    challenge_type="competing_hypotheses",
    domain_bounds=NumericalDomainBounds("x", -3.0, 3.0, "1d_polynomial"),
    max_query_budget=12,
    success_threshold=0.1,
    noise_std=0.0,
    held_out_inputs=tuple({"x": float(x)} for x in [-2.7, -2.0, -1.2, 1.2, 2.0, 2.7]),
    initial_inputs=({"x": 0.0},),
    ground_truth=PolynomialGroundTruthRule(
        domain="polynomial",
        target_formula="y = 3.0*x - 0.5*x^3",
        target_parameters={"degree": 3, "coefficients": {"c0": 0.0, "c1": 3.0, "c2": 0.0, "c3": -0.5}},
        evaluator_label="target_cubic_3x_minus_half_x3",
        secret_seed=2024,
    ),
    expected_baseline_queries={"random_sampling": 100, "passive_uniform": 75},
)

NOISY_POLYNOMIAL_BENCHMARK = BenchmarkCaseDefinition(
    benchmark_name="noisy_polynomial_benchmark",
    scenario_id="noisy_polynomial_benchmark",
    description="Discovery of linear relationship y = 1.0 + 2.0*x under Gaussian observation noise (std=0.15)",
    challenge_type="noisy",
    domain_bounds=NumericalDomainBounds("x", -5.0, 5.0, "1d_polynomial"),
    max_query_budget=10,
    success_threshold=0.25,
    noise_std=0.15,
    held_out_inputs=tuple({"x": float(x)} for x in [-4.0, -2.0, 1.0, 3.0]),
    initial_inputs=({"x": 0.0},),
    ground_truth=PolynomialGroundTruthRule(
        domain="polynomial",
        target_formula="y = 1.0 + 2.0*x",
        target_parameters={"degree": 1, "coefficients": {"c0": 1.0, "c1": 2.0}},
        evaluator_label="target_noisy_linear",
        secret_seed=777,
    ),
    expected_baseline_queries={"random_sampling": 40, "passive_uniform": 35},
)

BOUNDARY_EDGE_CASES_BENCHMARK = BenchmarkCaseDefinition(
    benchmark_name="boundary_edge_cases_benchmark",
    scenario_id="boundary_edge_cases_benchmark",
    description="Discovery with strict domain boundary inputs x in [-2.0, 2.0] for y = 2.0 + 1.5*x - 0.25*x^2",
    challenge_type="boundary_edge_cases",
    domain_bounds=NumericalDomainBounds("x", -2.0, 2.0, "1d_polynomial"),
    max_query_budget=10,
    success_threshold=0.08,
    noise_std=0.0,
    held_out_inputs=tuple({"x": float(x)} for x in [-1.95, -1.2, -0.5, 0.5, 1.2, 1.95]),
    initial_inputs=({"x": -1.95},),
    ground_truth=PolynomialGroundTruthRule(
        domain="polynomial",
        target_formula="y = 2.0 + 1.5*x - 0.25*x^2",
        target_parameters={"degree": 2, "coefficients": {"c0": 2.0, "c1": 1.5, "c2": -0.25}},
        evaluator_label="target_boundary_quad",
        secret_seed=888,
    ),
    expected_baseline_queries={"random_sampling": 50, "passive_uniform": 40},
)

ALL_BENCHMARK_CASES = [
    CONSTANT_BENCHMARK,
    LINEAR_BENCHMARK,
    QUADRATIC_BENCHMARK,
    CUBIC_BENCHMARK,
    NOISY_POLYNOMIAL_BENCHMARK,
    BOUNDARY_EDGE_CASES_BENCHMARK,
]

BENCHMARK_CASES: Dict[str, BenchmarkCaseDefinition] = {
    "polynomial_constant": CONSTANT_BENCHMARK,
    "polynomial_constant_benchmark": CONSTANT_BENCHMARK,
    "target_constant_c5": CONSTANT_BENCHMARK,
    "polynomial_linear": LINEAR_BENCHMARK,
    "polynomial_linear_benchmark": LINEAR_BENCHMARK,
    "linear_1d": LINEAR_BENCHMARK,
    "target_linear_2x_plus_1": LINEAR_BENCHMARK,
    "polynomial_quadratic": QUADRATIC_BENCHMARK,
    "polynomial_quadratic_benchmark": QUADRATIC_BENCHMARK,
    "quadratic_nonlinear": QUADRATIC_BENCHMARK,
    "target_quadratic_neg1_plus_half_x2": QUADRATIC_BENCHMARK,
    "polynomial_cubic": CUBIC_BENCHMARK,
    "polynomial_cubic_benchmark": CUBIC_BENCHMARK,
    "competing_cubic": CUBIC_BENCHMARK,
    "target_cubic_3x_minus_half_x3": CUBIC_BENCHMARK,
    "noisy_polynomial": NOISY_POLYNOMIAL_BENCHMARK,
    "noisy_polynomial_benchmark": NOISY_POLYNOMIAL_BENCHMARK,
    "boundary_edge_cases": BOUNDARY_EDGE_CASES_BENCHMARK,
    "boundary_edge_cases_benchmark": BOUNDARY_EDGE_CASES_BENCHMARK,
}


def get_benchmark_case(key: str) -> Optional[BenchmarkCaseDefinition]:
    """Retrieve a benchmark case definition by name, label, or alias."""
    return BENCHMARK_CASES.get(key.strip())


def list_benchmark_cases() -> List[str]:
    """List unique registered benchmark case names."""
    seen = set()
    cases = []
    for case in ALL_BENCHMARK_CASES:
        if case.benchmark_name not in seen:
            seen.add(case.benchmark_name)
            cases.append(case.benchmark_name)
    return cases


def list_benchmark_scenarios() -> List[Dict[str, Any]]:
    """List sanitized benchmark scenario descriptors safe for API consumption."""
    seen = set()
    scenarios = []
    for case in ALL_BENCHMARK_CASES:
        if case.benchmark_name not in seen:
            seen.add(case.benchmark_name)
            scenarios.append(case.to_sanitized_dict())
    return scenarios
