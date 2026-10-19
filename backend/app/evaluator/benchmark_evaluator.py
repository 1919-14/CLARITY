"""Isolated Benchmark Evaluator for CLARITY.

Operates OUTSIDE the discovery engine. Accesses reference ground truth
to compute Hit@K, MRR, and held-out prediction error without leaking
answers into discovery-facing objects.
"""

from typing import Dict, Any, List, Optional
from ..domain.contracts import (
    GroundTruthRule,
    HypothesisRepresentation,
    BenchmarkReport,
    generate_id,
)


class BenchmarkEvaluator:
    """Ground-truth evaluator harness.
    
    Receives hidden ground truth and discovery candidate outputs to calculate
    objective benchmark metrics.
    """

    def __init__(self, ground_truth: GroundTruthRule):
        self._ground_truth = ground_truth

    @property
    def target_label(self) -> str:
        return self._ground_truth.evaluator_label

    def evaluate_candidates(
        self,
        run_id: str,
        ranked_candidates: List[HypothesisRepresentation],
        queries_executed: Optional[int] = None,
        held_out_mae: Optional[float] = None,
        held_out_rmse: Optional[float] = None,
    ) -> BenchmarkReport:
        """Score candidate hypotheses against ground truth.
        
        Calculates:
        - hit_at_1, hit_at_3, hit_at_5: whether a candidate matching the target label appears in top K.
        - reciprocal_rank (MRR): 1 / rank of the target candidate.
        """
        hit_1 = False
        hit_3 = False
        hit_5 = False
        reciprocal_rank = 0.0
        best_candidate_id = ranked_candidates[0].hypothesis_id if ranked_candidates else "none"

        for idx, candidate in enumerate(ranked_candidates):
            rank = idx + 1
            # A candidate matches if its model family and core parameters align with target
            is_match = (
                candidate.model_family == self._ground_truth.domain
                or candidate.formula == self._ground_truth.target_formula
                or (
                    "degree" in candidate.parameters
                    and candidate.parameters.get("degree") == self._ground_truth.target_parameters.get("degree")
                )
            )

            if is_match:
                if rank == 1:
                    hit_1 = True
                if rank <= 3:
                    hit_3 = True
                if rank <= 5:
                    hit_5 = True
                reciprocal_rank = 1.0 / float(rank)
                break

        return BenchmarkReport(
            benchmark_id=generate_id("bench"),
            run_id=run_id,
            target_label=self._ground_truth.evaluator_label,
            hit_at_1=hit_1,
            hit_at_3=hit_3,
            hit_at_5=hit_5,
            reciprocal_rank=reciprocal_rank,
            best_candidate_id=best_candidate_id,
            queries_to_target=queries_executed,
            held_out_mae=held_out_mae,
            held_out_rmse=held_out_rmse,
        )
