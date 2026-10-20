"""Candidate experiment input generation for CLARITY (Issue #9).

Proposes valid, diverse, and reproducible candidate inputs for black-box oracle
experimentation without accessing hidden oracle rules or evaluator ground truth.

Key features:
1. CandidateExperiment: Standardized domain representation of an experiment proposal.
2. NumericalDomainBounds: 1D and multi-D box-bounded continuous domains with structured
   error validation, handling extreme integers without leaking OverflowError.
3. Multiple strategies: Deterministic grid-based (with even full-domain spatial coverage
   even after exclusions) and seeded uniform / stratified random generation.
4. Schema validation, bounds validation, and duplicate elimination.
5. Exclusion of previously queried inputs using pure absolute tolerance (rel_tol=0.0).
6. Precision preservation: Does not truncate or round inputs into violating bounds.
7. CandidateExperimentBatch: Structured result exposing requested vs generated counts
   and diagnostic warnings when undersized pools are produced.
8. Pure domain component: independent of database, network, oracle implementations, and evaluator.
"""

from dataclasses import dataclass, field
from enum import Enum
import math
import random
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .contracts import (
    ExperimentInput,
    StructuredError,
    generate_id,
    utc_now_iso,
    verify_no_ground_truth_leakage,
)

# Standard error codes
ERROR_INVALID_DOMAIN_BOUNDS = "INVALID_DOMAIN_BOUNDS"
ERROR_INVALID_EXPERIMENT_CONFIG = "INVALID_EXPERIMENT_CONFIG"
ERROR_OUT_OF_BOUNDS = "OUT_OF_BOUNDS"
ERROR_INVALID_INPUT_SCHEMA = "INVALID_INPUT_SCHEMA"
ERROR_EMPTY_CANDIDATE_POOL = "EMPTY_CANDIDATE_POOL"
ERROR_UNDERSIZED_CANDIDATE_POOL = "UNDERSIZED_CANDIDATE_POOL"
ERROR_EXHAUSTED_ATTEMPT_BUDGET = "EXHAUSTED_ATTEMPT_BUDGET"

DEFAULT_EXPERIMENT_POOL_SIZE = 10
MAX_EXPERIMENT_POOL_SIZE = 500


class CandidateExperimentStatus(str, Enum):
    ELIGIBLE = "eligible"
    EXCLUDED_PREVIOUSLY_QUERIED = "excluded_previously_queried"
    INVALID_BOUNDS = "invalid_bounds"
    REJECTED = "rejected"


class ExperimentGenerationStrategy(str, Enum):
    GRID = "grid"
    UNIFORM_RANDOM = "uniform_random"
    STRATIFIED_RANDOM = "stratified_random"


@dataclass(frozen=True)
class NumericalDomainBounds:
    """Specification of a bounded continuous numerical domain for independent variables.
    
    Example for 1D polynomial:
      variable_name: "x"
      min_val: -10.0
      max_val: 10.0
    """
    variable_name: str = "x"
    min_val: float = -10.0
    max_val: float = 10.0
    domain_name: str = "1d_polynomial"

    def __post_init__(self):
        if not self.variable_name or not isinstance(self.variable_name, str):
            raise ValueError("variable_name must be a non-empty string.")
        if not isinstance(self.min_val, (int, float)) or isinstance(self.min_val, bool):
            raise ValueError("min_val must be a real number.")
        if not isinstance(self.max_val, (int, float)) or isinstance(self.max_val, bool):
            raise ValueError("max_val must be a real number.")
        if not math.isfinite(self.min_val) or not math.isfinite(self.max_val):
            raise ValueError("Domain bounds must be finite.")
        if self.min_val > self.max_val:
            raise ValueError(f"min_val ({self.min_val}) cannot exceed max_val ({self.max_val}).")

    def contains(self, val: float) -> bool:
        """Check if scalar value lies within inclusive [min_val, max_val]."""
        return self.min_val <= val <= self.max_val

    def validate_value(self, val: Any) -> Optional[StructuredError]:
        """Validate input value type and bounds without leaking unhandled exceptions."""
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            return StructuredError(
                code=ERROR_INVALID_INPUT_SCHEMA,
                message=f"Variable '{self.variable_name}' must be a real number, got {type(val).__name__}.",
                details={"variable": self.variable_name, "value": str(val)},
            )

        # Safely convert to float; catch OverflowError for arbitrary-precision integers
        try:
            fval = float(val)
        except OverflowError:
            return StructuredError(
                code=ERROR_OUT_OF_BOUNDS,
                message=f"Variable '{self.variable_name}' value overflows standard float representation.",
                details={"variable": self.variable_name, "value": str(val)},
            )

        if not math.isfinite(fval):
            return StructuredError(
                code=ERROR_INVALID_INPUT_SCHEMA,
                message=f"Variable '{self.variable_name}' must be finite.",
                details={"variable": self.variable_name, "value": str(val)},
            )

        if not self.contains(fval):
            return StructuredError(
                code=ERROR_OUT_OF_BOUNDS,
                message=f"Variable '{self.variable_name}'={fval} is outside bounds [{self.min_val}, {self.max_val}].",
                details={
                    "variable": self.variable_name,
                    "value": fval,
                    "min_val": self.min_val,
                    "max_val": self.max_val,
                },
            )
        return None


@dataclass(frozen=True)
class CandidateExperiment:
    """An individual candidate experiment input proposed for oracle query evaluation.
    
    Fields:
      candidate_id: Unique stable identifier.
      input_data: Validated ExperimentInput payload.
      status: Candidate eligibility status.
      strategy: Strategy used to propose candidate.
      generation_metadata: Seed, sequence index, or step details.
      rejection_reason: Error explanation if status is not ELIGIBLE.
    """
    input_data: ExperimentInput
    candidate_id: str = field(default_factory=lambda: generate_id("cand-exp"))
    status: CandidateExperimentStatus = CandidateExperimentStatus.ELIGIBLE
    strategy: ExperimentGenerationStrategy = ExperimentGenerationStrategy.GRID
    generation_metadata: Optional[Dict[str, Any]] = None
    rejection_reason: Optional[StructuredError] = None
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self):
        # Enforce no ground truth leakage in metadata
        if self.generation_metadata:
            verify_no_ground_truth_leakage(self.generation_metadata)

    def canonical_key(self, round_digits: Optional[int] = None) -> Tuple[Tuple[str, Any], ...]:
        """Produce an immutable tuple key for duplicate detection and membership checks.
        
        If round_digits is provided, rounds floating point numbers; otherwise preserves exact value.
        """
        items = []
        for k, v in sorted(self.input_data.values.items()):
            if round_digits is not None and isinstance(v, (int, float)) and not isinstance(v, bool):
                items.append((k, round(float(v), round_digits)))
            else:
                items.append((k, v))
        return tuple(items)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "candidate_id": self.candidate_id,
            "input_data": self.input_data.to_dict(),
            "status": self.status.value,
            "strategy": self.strategy.value,
            "created_at": self.created_at,
        }
        if self.generation_metadata is not None:
            data["generation_metadata"] = self.generation_metadata
        if self.rejection_reason is not None:
            data["rejection_reason"] = self.rejection_reason.to_dict()
        return data


@dataclass(frozen=True)
class ExperimentGenerationConfig:
    """Configuration for candidate experiment generation.
    
    Fields:
      pool_size: Target number of valid candidate experiments to generate.
      strategy: GRID, UNIFORM_RANDOM, or STRATIFIED_RANDOM.
      bounds: Domain bounds specification.
      seed: Random seed for reproducible stochastic generation.
      exclude_inputs: Sequence of previously queried values to exclude.
      tolerance: Pure absolute tolerance for checking if an input was already queried.
      allow_undersized: Whether to return fewer candidates than pool_size or raise error.
      precision_digits: Optional rounding precision; None preserves full float precision.
    """
    pool_size: int = DEFAULT_EXPERIMENT_POOL_SIZE
    strategy: ExperimentGenerationStrategy = ExperimentGenerationStrategy.GRID
    bounds: NumericalDomainBounds = field(default_factory=NumericalDomainBounds)
    seed: Optional[int] = None
    exclude_inputs: Sequence[Dict[str, Any]] = field(default_factory=list)
    tolerance: float = 1e-5
    allow_undersized: bool = True
    precision_digits: Optional[int] = None

    def __post_init__(self):
        if not isinstance(self.pool_size, int) or isinstance(self.pool_size, bool):
            raise ValueError("pool_size must be an integer.")
        if self.pool_size < 1:
            raise ValueError(f"pool_size must be >= 1, got {self.pool_size}.")
        if self.pool_size > MAX_EXPERIMENT_POOL_SIZE:
            raise ValueError(f"pool_size cannot exceed {MAX_EXPERIMENT_POOL_SIZE}.")
        if self.seed is not None and not isinstance(self.seed, int):
            raise ValueError("seed must be an integer or None.")

        # Strict validation of tolerance
        if not isinstance(self.tolerance, (int, float)) or isinstance(self.tolerance, bool):
            raise ValueError("tolerance must be a real number.")
        if not math.isfinite(self.tolerance):
            raise ValueError("tolerance must be finite.")
        if self.tolerance < 0:
            raise ValueError("tolerance must be non-negative.")

        # Validate precision_digits if specified
        if self.precision_digits is not None:
            if not isinstance(self.precision_digits, int) or isinstance(self.precision_digits, bool) or self.precision_digits < 0:
                raise ValueError("precision_digits must be a non-negative integer or None.")


@dataclass(frozen=True)
class CandidateExperimentBatch:
    """Container holding generated candidate experiments and execution diagnostics.
    
    Addresses non-blocking requirement to expose requested vs returned counts and diagnostics.
    """
    candidates: List[CandidateExperiment]
    requested_count: int
    generated_count: int
    is_undersized: bool = False
    diagnostic_message: Optional[str] = None

    def __iter__(self):
        return iter(self.candidates)

    def __len__(self):
        return len(self.candidates)

    def __getitem__(self, index):
        return self.candidates[index]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_count": self.requested_count,
            "generated_count": self.generated_count,
            "is_undersized": self.is_undersized,
            "diagnostic_message": self.diagnostic_message,
            "candidates": [c.to_dict() for c in self.candidates],
        }


class CandidateExperimentGenerationError(Exception):
    """Exception raised when candidate experiment generation fails."""

    def __init__(self, error: StructuredError):
        super().__init__(f"[{error.code}] {error.message}")
        self.error = error


class CandidateExperimentGenerator:
    """Generates candidate experiment pools for black-box systems."""

    def __init__(self, config: Optional[ExperimentGenerationConfig] = None):
        self.config = config or ExperimentGenerationConfig()

    def generate_batch(self, config_override: Optional[ExperimentGenerationConfig] = None) -> CandidateExperimentBatch:
        """Generate a CandidateExperimentBatch including candidates and diagnostics."""
        cfg = config_override or self.config
        bounds = cfg.bounds

        # Check for degenerate domain
        if bounds.min_val == bounds.max_val and cfg.pool_size > 1 and not cfg.allow_undersized:
            raise CandidateExperimentGenerationError(
                StructuredError(
                    code=ERROR_UNDERSIZED_CANDIDATE_POOL,
                    message=f"Domain bounds are degenerate [{bounds.min_val}, {bounds.max_val}]; cannot produce {cfg.pool_size} unique candidates.",
                    details={"min_val": bounds.min_val, "max_val": bounds.max_val, "pool_size": cfg.pool_size},
                )
            )

        # Build exclusion set of floats
        excluded_scalars: List[float] = []
        for exc in cfg.exclude_inputs:
            if isinstance(exc, dict) and bounds.variable_name in exc:
                v = exc[bounds.variable_name]
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    try:
                        excluded_scalars.append(float(v))
                    except OverflowError:
                        pass

        # Dispatch generation strategy
        if cfg.strategy == ExperimentGenerationStrategy.GRID:
            raw_candidates = self._generate_grid(cfg, excluded_scalars)
        elif cfg.strategy == ExperimentGenerationStrategy.UNIFORM_RANDOM:
            raw_candidates = self._generate_uniform_random(cfg, excluded_scalars)
        elif cfg.strategy == ExperimentGenerationStrategy.STRATIFIED_RANDOM:
            raw_candidates = self._generate_stratified_random(cfg, excluded_scalars)
        else:
            raise ValueError(f"Unsupported generation strategy: {cfg.strategy}")

        # Filter, deduplicate, and validate candidates
        valid_candidates: List[CandidateExperiment] = []
        seen_keys: Set[Tuple[Tuple[str, Any], ...]] = set()

        for cand in raw_candidates:
            raw_val = cand.input_data.values.get(bounds.variable_name)
            # 1. Schema & bounds check
            val_err = bounds.validate_value(raw_val)
            if val_err is not None:
                continue

            # 2. Check if previously queried using pure absolute tolerance (rel_tol=0.0)
            fval = float(raw_val)
            if any(math.isclose(fval, ex, rel_tol=0.0, abs_tol=cfg.tolerance) for ex in excluded_scalars):
                continue

            # 3. Batch deduplication
            key = cand.canonical_key(cfg.precision_digits)
            if key in seen_keys:
                continue

            seen_keys.add(key)
            valid_candidates.append(cand)

            if len(valid_candidates) == cfg.pool_size:
                break

        # Check pool size constraints
        if not valid_candidates:
            raise CandidateExperimentGenerationError(
                StructuredError(
                    code=ERROR_EMPTY_CANDIDATE_POOL,
                    message="Candidate experiment pool is empty after applying domain bounds and exclusion filters.",
                    details={"pool_size_requested": cfg.pool_size, "excluded_count": len(excluded_scalars)},
                )
            )

        is_undersized = len(valid_candidates) < cfg.pool_size
        if is_undersized and not cfg.allow_undersized:
            raise CandidateExperimentGenerationError(
                StructuredError(
                    code=ERROR_UNDERSIZED_CANDIDATE_POOL,
                    message=f"Generated {len(valid_candidates)} candidates, fewer than requested pool size {cfg.pool_size}.",
                    details={
                        "generated_count": len(valid_candidates),
                        "requested_count": cfg.pool_size,
                        "strategy": cfg.strategy.value,
                    },
                )
            )

        diag_msg = None
        if is_undersized:
            diag_msg = (
                f"Generated {len(valid_candidates)} candidate experiments, fewer than requested {cfg.pool_size} "
                f"due to domain bounds [{bounds.min_val}, {bounds.max_val}] or {len(excluded_scalars)} excluded input(s)."
            )

        return CandidateExperimentBatch(
            candidates=valid_candidates,
            requested_count=cfg.pool_size,
            generated_count=len(valid_candidates),
            is_undersized=is_undersized,
            diagnostic_message=diag_msg,
        )

    def generate(self, config_override: Optional[ExperimentGenerationConfig] = None) -> List[CandidateExperiment]:
        """Convenience method returning list of CandidateExperiment instances for backward compatibility."""
        batch = self.generate_batch(config_override)
        return batch.candidates

    def _format_value(self, val: float, cfg: ExperimentGenerationConfig) -> float:
        """Format value according to precision policy, guaranteeing bounds adherence."""
        bounds = cfg.bounds
        if cfg.precision_digits is not None:
            formatted = round(val, cfg.precision_digits)
            # If rounding nudged point outside bounds, clamp to bound
            if formatted < bounds.min_val:
                formatted = bounds.min_val
            elif formatted > bounds.max_val:
                formatted = bounds.max_val
            return formatted
        return val

    def _generate_grid(
        self,
        cfg: ExperimentGenerationConfig,
        excluded_scalars: List[float],
    ) -> List[CandidateExperiment]:
        """Generate grid points distributed evenly across [min_val, max_val].
        
        To prevent exclusions from skewing the grid toward the lower end, we dynamically
        subsample from an available eligible point grid or iteratively space across the remaining domain.
        """
        bounds = cfg.bounds
        n = cfg.pool_size
        # Over-sample densely across the whole domain so points remain evenly distributed across full range
        oversample = max(n * 5, n + len(excluded_scalars) * 10, 50)
        candidates = []

        if bounds.min_val == bounds.max_val:
            pts = [bounds.min_val]
        else:
            step = (bounds.max_val - bounds.min_val) / (oversample - 1)
            pts = [bounds.min_val + i * step for i in range(oversample)]

        # Filter out points matching exclusion set to find eligible pool
        eligible_pts = []
        for pt in pts:
            fpt = self._format_value(pt, cfg)
            if not any(math.isclose(fpt, ex, rel_tol=0.0, abs_tol=cfg.tolerance) for ex in excluded_scalars):
                eligible_pts.append(fpt)

        # Select n evenly spaced samples across the entire eligible array to maintain global coverage
        if len(eligible_pts) <= n:
            selected_pts = eligible_pts
        else:
            # Subsample evenly from beginning to end of eligible_pts
            indices = [int(round(i * (len(eligible_pts) - 1) / (n - 1))) for i in range(n)]
            # Preserve uniqueness of indices
            unique_indices = sorted(list(dict.fromkeys(indices)))
            selected_pts = [eligible_pts[idx] for idx in unique_indices]

        for idx, pt in enumerate(selected_pts):
            inp = ExperimentInput(
                values={bounds.variable_name: pt},
                domain=bounds.domain_name,
                metadata={"strategy": cfg.strategy.value, "grid_index": idx},
            )
            cand = CandidateExperiment(
                input_data=inp,
                strategy=cfg.strategy,
                generation_metadata={
                    "strategy": cfg.strategy.value,
                    "grid_index": idx,
                    "point": pt,
                },
            )
            candidates.append(cand)

        return candidates

    def _generate_uniform_random(
        self,
        cfg: ExperimentGenerationConfig,
        excluded_scalars: List[float],
    ) -> List[CandidateExperiment]:
        """Generate uniform random candidate inputs within bounds using seeded RNG."""
        bounds = cfg.bounds
        rng = random.Random(cfg.seed)
        candidates = []
        max_attempts = cfg.pool_size * 50

        for attempt in range(max_attempts):
            raw_val = rng.uniform(bounds.min_val, bounds.max_val)
            val = self._format_value(raw_val, cfg)
            inp = ExperimentInput(
                values={bounds.variable_name: val},
                domain=bounds.domain_name,
                metadata={"strategy": cfg.strategy.value, "attempt": attempt},
            )
            cand = CandidateExperiment(
                input_data=inp,
                strategy=cfg.strategy,
                generation_metadata={
                    "strategy": cfg.strategy.value,
                    "seed": cfg.seed,
                    "attempt": attempt,
                },
            )
            candidates.append(cand)
            if len(candidates) >= cfg.pool_size * 5:
                break

        return candidates

    def _generate_stratified_random(
        self,
        cfg: ExperimentGenerationConfig,
        excluded_scalars: List[float],
    ) -> List[CandidateExperiment]:
        """Generate stratified random samples across equal intervals in bounds."""
        bounds = cfg.bounds
        rng = random.Random(cfg.seed)
        n = cfg.pool_size + len(excluded_scalars) * 2
        candidates = []

        if bounds.min_val == bounds.max_val:
            step = 0.0
        else:
            step = (bounds.max_val - bounds.min_val) / max(1, n)

        for i in range(n):
            stratum_min = bounds.min_val + i * step
            stratum_max = bounds.min_val + (i + 1) * step if step > 0 else stratum_min
            raw_val = rng.uniform(stratum_min, stratum_max) if step > 0 else stratum_min
            val = self._format_value(raw_val, cfg)
            inp = ExperimentInput(
                values={bounds.variable_name: val},
                domain=bounds.domain_name,
                metadata={"strategy": cfg.strategy.value, "stratum": i},
            )
            cand = CandidateExperiment(
                input_data=inp,
                strategy=cfg.strategy,
                generation_metadata={
                    "strategy": cfg.strategy.value,
                    "seed": cfg.seed,
                    "stratum": i,
                },
            )
            candidates.append(cand)

        return candidates


def generate_candidate_experiments(
    pool_size: int = DEFAULT_EXPERIMENT_POOL_SIZE,
    bounds: Optional[NumericalDomainBounds] = None,
    strategy: ExperimentGenerationStrategy = ExperimentGenerationStrategy.GRID,
    seed: Optional[int] = None,
    exclude_inputs: Optional[Sequence[Dict[str, Any]]] = None,
    tolerance: float = 1e-5,
    allow_undersized: bool = True,
    precision_digits: Optional[int] = None,
) -> List[CandidateExperiment]:
    """Functional convenience entrypoint to generate candidate experiments."""
    cfg = ExperimentGenerationConfig(
        pool_size=pool_size,
        bounds=bounds or NumericalDomainBounds(),
        strategy=strategy,
        seed=seed,
        exclude_inputs=exclude_inputs or [],
        tolerance=tolerance,
        allow_undersized=allow_undersized,
        precision_digits=precision_digits,
    )
    generator = CandidateExperimentGenerator(cfg)
    return generator.generate()
