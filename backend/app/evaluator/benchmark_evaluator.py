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

    def __init__(self, ground_truth: GroundTruthRule, tolerance: float = 0.05):
        self._ground_truth = ground_truth
        self._tolerance = tolerance

    @property
    def target_label(self) -> str:
        return self._ground_truth.evaluator_label

    def is_match(self, candidate: HypothesisRepresentation) -> bool:
        """Check if candidate hypothesis matches the evaluator ground truth."""
        # 1. Custom matching logic on rule if provided (e.g. PolynomialGroundTruthRule)
        if hasattr(self._ground_truth, "matches") and callable(getattr(self._ground_truth, "matches")):
            return bool(self._ground_truth.matches(candidate, tolerance=self._tolerance))

        # 2. Exact normalized formula match
        cand_formula = (candidate.formula or "").replace(" ", "").lower()
        tgt_formula = (self._ground_truth.target_formula or "").replace(" ", "").lower()
        if cand_formula and tgt_formula and cand_formula == tgt_formula:
            return True

        # 3. Model family compatibility check
        cand_family = (candidate.model_family or "").lower()
        tgt_domain = (self._ground_truth.domain or "").lower()
        if tgt_domain and cand_family:
            if tgt_domain not in cand_family and cand_family not in tgt_domain:
                return False

        # 4. Degree check if specified in both
        cand_params = candidate.parameters or {}
        tgt_params = self._ground_truth.target_parameters or {}
        if "degree" in tgt_params and "degree" in cand_params:
            if int(tgt_params["degree"]) != int(cand_params["degree"]):
                return False

        # 5. Numerical parameter comparison
        if tgt_params:
            for k, tgt_val in tgt_params.items():
                if k in ("degree", "family", "coefficient_order"):
                    continue
                if isinstance(tgt_val, (int, float)):
                    cand_val = cand_params.get(k)
                    if cand_val is None or not isinstance(cand_val, (int, float)):
                        return False
                    if abs(float(tgt_val) - float(cand_val)) > self._tolerance:
                        return False
                elif isinstance(tgt_val, dict):
                    cand_dict = cand_params.get(k)
                    if not isinstance(cand_dict, dict):
                        return False
                    for sub_k, sub_val in tgt_val.items():
                        if isinstance(sub_val, (int, float)):
                            cand_sub_val = cand_dict.get(sub_k)
                            if cand_sub_val is None or not isinstance(cand_sub_val, (int, float)):
                                return False
                            if abs(float(sub_val) - float(cand_sub_val)) > self._tolerance:
                                return False

        return False

    def evaluate_candidates(
        self,
        run_id: str,
        ranked_candidates: List[HypothesisRepresentation],
        queries_executed: Optional[int] = None,
        held_out_mae: Optional[float] = None,
        held_out_rmse: Optional[float] = None,
        benchmark_name: Optional[str] = None,
        baseline_type: Optional[str] = None,
        efficiency_gain: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> BenchmarkReport:
        """Score candidate hypotheses against ground truth.
        
        Calculates:
        - hit_at_1, hit_at_3, hit_at_5: whether a candidate matching the target appears in top K.
        - reciprocal_rank (MRR): 1 / rank of the first matching candidate (0.0 if not found).
        - best_candidate_id: ID of the matching candidate or top-ranked candidate.
        """
        hit_1 = False
        hit_3 = False
        hit_5 = False
        reciprocal_rank = 0.0
        best_candidate_id = ranked_candidates[0].hypothesis_id if ranked_candidates else "none"

        for idx, candidate in enumerate(ranked_candidates):
            rank = idx + 1
            if self.is_match(candidate):
                if rank == 1:
                    hit_1 = True
                if rank <= 3:
                    hit_3 = True
                if rank <= 5:
                    hit_5 = True
                reciprocal_rank = 1.0 / float(rank)
                best_candidate_id = candidate.hypothesis_id
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
            benchmark_name=benchmark_name,
            baseline_type=baseline_type,
            efficiency_gain=efficiency_gain,
            metadata=metadata,
        )
