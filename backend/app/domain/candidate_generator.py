"""Candidate hypothesis generation for CLARITY (Issue #5).

The generator is a pure domain component. It depends only on the domain
contracts and the model-family registry: it never receives an oracle instance,
evaluator objects, ground-truth data, credentials, or network/database access.

Generated candidates are *proposals*. Their parameters are an initialization
policy, not a fit, and nothing here claims accuracy, confidence, or rank.
"""

import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .contracts import (
    HypothesisOrigin,
    HypothesisProvenance,
    HypothesisRepresentation,
    HypothesisStatus,
    StructuredError,
)
from .hypothesis_models import (
    COEFFICIENT_ORDER_ASCENDING,
    DEFAULT_MAX_POLYNOMIAL_DEGREE,
    ERROR_UNKNOWN_MODEL_FAMILY,
    ERROR_UNSUPPORTED_DEGREE,
    HARD_MAX_POLYNOMIAL_DEGREE,
    POLYNOMIAL_FAMILY_ID,
    ModelFamilyRegistry,
    ValidatedModel,
    default_registry,
)

GENERATOR_NAME = "polynomial_candidate_generator"
GENERATOR_VERSION = "1.0.0"

DEFAULT_CANDIDATE_COUNT = 6
# Upper bound on one batch so a bad configuration cannot request an unbounded
# number of candidates.
MAX_CANDIDATE_COUNT = 64

ERROR_INVALID_GENERATION_CONFIG = "INVALID_GENERATION_CONFIG"
# Bounded sampling ran out of attempts. This is deliberately not named
# "candidate space exhausted": reaching the attempt limit does not prove that no
# further unique candidate exists.
ERROR_ATTEMPT_BUDGET_EXHAUSTED = "ATTEMPT_BUDGET_EXHAUSTED"

# Documented initialization labels recorded in generation_metadata.
INITIALIZATION_BASELINE = "deterministic_monomial_baseline"
INITIALIZATION_RANDOM = "seeded_uniform_random"


class CandidateGenerationError(Exception):
    """Raised when candidate generation cannot produce the requested candidates."""

    def __init__(self, error: StructuredError):
        super().__init__(f"[{error.code}] {error.message}")
        self.error = error


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_real_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class HypothesisGenerationConfig:
    """Validated inputs for one generation batch.

    Fields:
      family_id: Registered model family to sample from.
      min_degree: Lowest degree to propose (inclusive).
      max_degree: Highest degree to propose (inclusive); must not exceed the
        family's configured maximum.
      candidate_count: Number of unique candidates to return.
      seed: Seed for the parameter draws. None means non-reproducible output.
      coefficient_bound: Uniform coefficient draws use [-bound, bound]. 0.0 is a
        degenerate configuration that only yields zero coefficients.
      max_attempts_per_candidate: Attempt budget multiplier before the generator
        reports an exhausted candidate space.
    """

    family_id: str = POLYNOMIAL_FAMILY_ID
    min_degree: int = 0
    max_degree: int = DEFAULT_MAX_POLYNOMIAL_DEGREE
    candidate_count: int = DEFAULT_CANDIDATE_COUNT
    seed: Optional[int] = None
    coefficient_bound: float = 1.0
    max_attempts_per_candidate: int = 16

    def to_dict(self) -> Dict[str, Any]:
        return {
            "family_id": self.family_id,
            "min_degree": self.min_degree,
            "max_degree": self.max_degree,
            "candidate_count": self.candidate_count,
            "seed": self.seed,
            "coefficient_bound": self.coefficient_bound,
            "max_attempts_per_candidate": self.max_attempts_per_candidate,
        }

    def validate(self) -> Optional[StructuredError]:
        """Return a structured error when the configuration is unusable."""
        if not isinstance(self.family_id, str) or not self.family_id:
            return StructuredError(
                code=ERROR_INVALID_GENERATION_CONFIG,
                message="family_id must be a non-empty string.",
                details={"family_id": str(self.family_id)},
            )
        if not _is_int(self.candidate_count) or not 1 <= self.candidate_count <= MAX_CANDIDATE_COUNT:
            return StructuredError(
                code=ERROR_INVALID_GENERATION_CONFIG,
                message=(
                    f"candidate_count must be an integer between 1 and {MAX_CANDIDATE_COUNT}, "
                    f"got {self.candidate_count!r}."
                ),
                details={"candidate_count": str(self.candidate_count)},
            )
        if not _is_int(self.min_degree) or not _is_int(self.max_degree):
            return StructuredError(
                code=ERROR_INVALID_GENERATION_CONFIG,
                message="min_degree and max_degree must be integers.",
                details={"min_degree": str(self.min_degree), "max_degree": str(self.max_degree)},
            )
        if self.min_degree < 0 or self.max_degree < self.min_degree:
            return StructuredError(
                code=ERROR_INVALID_GENERATION_CONFIG,
                message=(
                    "Degree limits must satisfy 0 <= min_degree <= max_degree, got "
                    f"min_degree={self.min_degree}, max_degree={self.max_degree}."
                ),
                details={"min_degree": self.min_degree, "max_degree": self.max_degree},
            )
        if self.max_degree > HARD_MAX_POLYNOMIAL_DEGREE:
            return StructuredError(
                code=ERROR_INVALID_GENERATION_CONFIG,
                message=(
                    f"max_degree must not exceed the supported ceiling "
                    f"{HARD_MAX_POLYNOMIAL_DEGREE}, got {self.max_degree}."
                ),
                details={"max_degree": self.max_degree},
            )
        if not _is_real_number(self.coefficient_bound) or not (
            float(self.coefficient_bound) >= 0.0 and float(self.coefficient_bound) != float("inf")
        ):
            return StructuredError(
                code=ERROR_INVALID_GENERATION_CONFIG,
                message="coefficient_bound must be a finite number greater than or equal to 0.",
                details={"coefficient_bound": str(self.coefficient_bound)},
            )
        if not _is_int(self.max_attempts_per_candidate) or self.max_attempts_per_candidate < 1:
            return StructuredError(
                code=ERROR_INVALID_GENERATION_CONFIG,
                message="max_attempts_per_candidate must be an integer greater than or equal to 1.",
                details={"max_attempts_per_candidate": str(self.max_attempts_per_candidate)},
            )
        if self.seed is not None and not _is_int(self.seed):
            return StructuredError(
                code=ERROR_INVALID_GENERATION_CONFIG,
                message=f"seed must be an integer or None, got {type(self.seed).__name__}.",
                details={"seed_type": type(self.seed).__name__},
            )
        return None


class CandidateGenerator:
    """Generates unique, validated candidate hypotheses within explicit limits."""

    generator_name = GENERATOR_NAME
    generator_version = GENERATOR_VERSION

    def __init__(
        self,
        config: HypothesisGenerationConfig,
        registry: Optional[ModelFamilyRegistry] = None,
    ):
        if not isinstance(config, HypothesisGenerationConfig):
            raise CandidateGenerationError(
                StructuredError(
                    code=ERROR_INVALID_GENERATION_CONFIG,
                    message=(
                        "CandidateGenerator requires a HypothesisGenerationConfig, got "
                        f"{type(config).__name__}."
                    ),
                )
            )
        self._config = config
        # Validate the configuration before constructing any default registry so
        # an out-of-range degree is reported as a structured generation error
        # rather than a raw constructor ValueError.
        config_error = config.validate()
        if config_error is not None:
            raise CandidateGenerationError(config_error)
        self._registry = registry if registry is not None else default_registry(config.max_degree)

        family = self._registry.get(config.family_id)
        if family is None:
            raise CandidateGenerationError(
                StructuredError(
                    code=ERROR_UNKNOWN_MODEL_FAMILY,
                    message=f"Model family {config.family_id!r} is not registered.",
                    details={"available_families": list(self._registry.family_ids())},
                )
            )
        family_max_degree = family.max_complexity - 1
        if config.max_degree > family_max_degree:
            raise CandidateGenerationError(
                StructuredError(
                    code=ERROR_UNSUPPORTED_DEGREE,
                    message=(
                        f"Configured max_degree {config.max_degree} exceeds the "
                        f"'{family.family_id}' family maximum of {family_max_degree}."
                    ),
                    details={"max_degree": config.max_degree, "family_max_degree": family_max_degree},
                )
            )
        self._family = family

    @property
    def config(self) -> HypothesisGenerationConfig:
        return self._config

    def generate(self) -> List[HypothesisRepresentation]:
        """Return ``candidate_count`` unique, validated candidate hypotheses."""
        config = self._config
        candidates: List[HypothesisRepresentation] = []
        seen: set = set()
        rng = random.Random(config.seed)

        # Phase 1: one deterministic, analytically simple baseline per degree.
        # This guarantees interpretable candidates and a stable leading order.
        for degree in range(config.min_degree, config.max_degree + 1):
            if len(candidates) >= config.candidate_count:
                break
            coefficients = [0.0] * (degree + 1)
            if degree >= 1:
                coefficients[degree] = 1.0
            self._try_add(candidates, seen, degree, coefficients, INITIALIZATION_BASELINE)

        # Phase 2: seeded parameter draws until the requested count is reached.
        attempts = 0
        attempt_budget = config.max_attempts_per_candidate * config.candidate_count
        while len(candidates) < config.candidate_count:
            if attempts >= attempt_budget:
                raise CandidateGenerationError(
                    StructuredError(
                        code=ERROR_ATTEMPT_BUDGET_EXHAUSTED,
                        message=(
                            f"Attempt budget exhausted: generated {len(candidates)} of "
                            f"{config.candidate_count} unique candidates after {attempts} of "
                            f"{attempt_budget} attempts. Bounded sampling did not find another "
                            "unique candidate; this does not prove the candidate space is "
                            "finite or empty."
                        ),
                        details={
                            "requested": config.candidate_count,
                            "generated": len(candidates),
                            "attempts": attempts,
                            "attempt_budget": attempt_budget,
                            "configuration": config.to_dict(),
                        },
                    )
                )
            attempts += 1
            degree = rng.randint(config.min_degree, config.max_degree)
            self._try_add(
                candidates, seen, degree, self._draw_coefficients(rng, degree), INITIALIZATION_RANDOM
            )

        return candidates

    def _draw_coefficients(self, rng: random.Random, degree: int) -> List[float]:
        bound = float(self._config.coefficient_bound)
        coefficients = [rng.uniform(-bound, bound) for _ in range(degree + 1)]
        # A non-zero leading coefficient is required for degree >= 1; the draw is
        # continuous so this fallback is effectively unreachable for bound > 0.
        if degree >= 1 and coefficients[degree] == 0.0 and bound > 0.0:
            coefficients[degree] = bound
        return coefficients

    def _try_add(
        self,
        candidates: List[HypothesisRepresentation],
        seen: set,
        degree: int,
        coefficients: List[float],
        initialization: str,
    ) -> bool:
        specification = {
            "family": self._family.family_id,
            "degree": degree,
            "coefficient_order": COEFFICIENT_ORDER_ASCENDING,
            "coefficients": {f"c{index}": value for index, value in enumerate(coefficients)},
        }
        model, error = self._family.parse_specification(specification)
        if error is not None:
            # Degenerate draws (for example coefficient_bound=0 with degree>=1)
            # are rejected by the family validator and simply skipped.
            return False

        canonical_key = model.canonical_key()
        if canonical_key in seen:
            return False

        seen.add(canonical_key)
        candidates.append(self._build_hypothesis(model, initialization, len(candidates)))
        return True

    def _build_hypothesis(
        self,
        model: ValidatedModel,
        initialization: str,
        sequence_index: int,
    ) -> HypothesisRepresentation:
        config = self._config
        return HypothesisRepresentation(
            model_family=self._family.family_id,
            formula=model.summarize(),
            parameters=model.to_specification(),
            complexity=model.complexity(),
            status=HypothesisStatus.CANDIDATE,
            provenance=HypothesisProvenance(
                origin=HypothesisOrigin.SYSTEM_GENERATED,
                generator=self.generator_name,
                generator_version=self.generator_version,
            ),
            generation_metadata={
                "generator": self.generator_name,
                "generator_version": self.generator_version,
                "seed": config.seed,
                "configuration": config.to_dict(),
                "initialization": initialization,
                "sequence_index": sequence_index,
            },
        )


def generate_candidates(
    config: HypothesisGenerationConfig,
    registry: Optional[ModelFamilyRegistry] = None,
) -> List[HypothesisRepresentation]:
    """Convenience wrapper around CandidateGenerator.generate()."""
    return CandidateGenerator(config, registry=registry).generate()
