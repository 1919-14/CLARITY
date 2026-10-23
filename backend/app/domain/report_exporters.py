"""Report exporters for CLARITY scientific discovery reports (Issue #23).

Supports:
- Structured machine-readable JSON export
- Formatted, human-readable scientific Markdown export

Requirements:
- Escape user-provided text safely in Markdown.
- Ensure unavailable fields remain explicitly unavailable (e.g. "Unavailable" or null, never zero).
- Prevent export from exposing credentials, tokens, or ground-truth secrets.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from .report_contracts import ReportHypothesis, ScientificReport
from .sanitization import sanitize_metadata


def _escape_md(text: Any) -> str:
    """Escape pipe characters and backslashes for Markdown tables."""
    if text is None:
        return "Unavailable"
    s = str(text)
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def export_report_as_json(report: ScientificReport) -> str:
    """Export the scientific report as formatted, machine-readable JSON."""
    raw_dict = report.to_dict()
    sanitized = sanitize_metadata(raw_dict)
    return json.dumps(sanitized, indent=2, ensure_ascii=False)


def export_report_as_markdown(report: ScientificReport) -> str:
    """Export the scientific report as a publication-ready Markdown document."""
    s = report.run_summary
    dp = report.discovery_process
    m = report.evaluation_methodology
    cu = report.complexity_and_uncertainty
    be = report.budget_and_execution
    ne = report.next_experiment

    lines = []

    # Title & Banner
    lines.append(f"# Scientific Discovery Report: {s.name}")
    lines.append(f"**Run ID:** `{report.run_id}` | **Report ID:** `{report.report_id}` | **Generated At:** `{report.generated_at}`")
    lines.append("")

    if s.is_partial:
        lines.append("> [!WARNING]")
        lines.append(f"> **Partial / Incomplete Run Notice**: This run terminated prematurely with status **{s.status.upper()}** "
                     f"(Reason: *{s.stop_reason or 'Stopped early'}*). "
                     "Findings in this report represent intermediate candidate evaluations and not a fully completed discovery search.")
        lines.append("")

    # 1. Run Summary
    lines.append("## 1. Run Summary")
    lines.append("")
    lines.append("| Property | Value |")
    lines.append("|---|---|")
    lines.append(f"| **Domain** | {_escape_md(s.domain)} |")
    lines.append(f"| **Execution Mode** | {_escape_md(s.execution_mode)} |")
    lines.append(f"| **Sampling Strategy** | {_escape_md(s.sampling_strategy)} |")
    lines.append(f"| **Final Status** | `{_escape_md(s.status)}` |")
    lines.append(f"| **Stop Reason** | {_escape_md(s.stop_reason)} |")
    lines.append(f"| **Started At** | {_escape_md(s.started_at)} |")
    lines.append(f"| **Completed At** | {_escape_md(s.completed_at)} |")
    dur_str = f"{s.duration_seconds:.2f} s" if s.duration_seconds is not None else "Unavailable"
    lines.append(f"| **Duration** | {dur_str} |")
    lines.append("")

    # 2. Discovery Process
    lines.append("## 2. Discovery Process")
    lines.append("")
    lines.append(f"{dp.search_progress}")
    lines.append("")
    lines.append("| Metric | Count |")
    lines.append("|---|---|")
    lines.append(f"| **Oracle Queries Executed** | {dp.oracle_queries_used} |")
    lines.append(f"| **Candidate Evaluations Performed** | {dp.candidate_evaluations_performed} |")
    lines.append(f"| **Hypotheses Formulated** | {dp.hypotheses_generated} |")
    lines.append("")

    # 3. Evaluation Methodology & Ranking Criteria
    lines.append("## 3. Evaluation Methodology")
    lines.append("")
    lines.append(f"- **Primary Metric:** `{m.primary_metric.upper()}` (minimization target, lower error indicates higher empirical agreement).")
    lines.append(f"- **Held-Out Evaluation Protocol:** {m.held_out_evaluation_approach}")
    lines.append(f"- **Dataset Version:** {m.dataset_version if m.dataset_version is not None else 'Unavailable'}")
    lines.append("")
    lines.append(f"**Ranking Criteria & Honesty:** {m.ranking_criteria}")
    lines.append("")

    # 4. Ranked Hypotheses
    lines.append("## 4. Top Ranked Hypotheses")
    lines.append("")
    if not report.ranked_hypotheses:
        lines.append("*No eligible candidate hypotheses were available for ranking in this run.*")
        lines.append("")
    else:
        lines.append("| Rank | Hypothesis ID | Family | Mathematical Expression | Held-Out Error | Training Error | Status |")
        lines.append("|---|---|---|---|---|---|---|")
        for h in report.ranked_hypotheses:
            held_out_str = f"{h.held_out_score:.4f}" if h.held_out_score is not None else "Unavailable"
            train_str = f"{h.training_score:.4f}" if h.training_score is not None else "Unavailable"
            lines.append(
                f"| {h.rank} | `{_escape_md(h.hypothesis_id)}` | {_escape_md(h.model_family)} | "
                f"`{_escape_md(h.formula)}` | {held_out_str} | {train_str} | `{_escape_md(h.status)}` |"
            )
        lines.append("")

    # 5. Supporting and Contradicting Evidence per Hypothesis
    lines.append("## 5. Evidence & Provenance")
    lines.append("")
    for h in report.ranked_hypotheses:
        lines.append(f"### Candidate {h.rank}: `{h.hypothesis_id}` ({h.formula})")
        lines.append(f"- **Scientific Plausibility Status:** `{h.status}` ({h.explanation})")
        lines.append(f"- **Evaluation Tier:** `{h.evaluation_tier}` (Dataset: `{h.dataset_id or 'unknown'}`)")
        lines.append(f"- **Complexity Score:** {h.complexity_score if h.complexity_score is not None else 'Unavailable'}")
        lines.append("")

        if h.supporting_evidence:
            lines.append(f"**Supporting Observations ({len(h.supporting_evidence)}):**")
            lines.append("| Observation ID | Partition | Observed y | Predicted y | Residual |")
            lines.append("|---|---|---|---|---|")
            for ev in h.supporting_evidence[:5]:  # Show top 5
                part = "held-out" if ev.is_held_out else "training"
                obs_y = f"{ev.observed_value:.4f}" if ev.observed_value is not None else "Unavailable"
                pred_y = f"{ev.predicted_value:.4f}" if ev.predicted_value is not None else "Unavailable"
                res_y = f"{ev.residual:.4f}" if ev.residual is not None else "Unavailable"
                lines.append(f"| `{_escape_md(ev.observation_id)}` | {part} | {obs_y} | {pred_y} | {res_y} |")
            lines.append("")

        if h.contradicting_evidence:
            lines.append(f"**Contradicting Observations ({len(h.contradicting_evidence)}):**")
            lines.append("| Observation ID | Partition | Observed y | Predicted y | Residual |")
            lines.append("|---|---|---|---|---|")
            for ev in h.contradicting_evidence[:5]:
                part = "held-out" if ev.is_held_out else "training"
                obs_y = f"{ev.observed_value:.4f}" if ev.observed_value is not None else "Unavailable"
                pred_y = f"{ev.predicted_value:.4f}" if ev.predicted_value is not None else "Unavailable"
                res_y = f"{ev.residual:.4f}" if ev.residual is not None else "Unavailable"
                lines.append(f"| `{_escape_md(ev.observation_id)}` | {part} | {obs_y} | {pred_y} | {res_y} |")
            lines.append("")

        if not h.supporting_evidence and not h.contradicting_evidence:
            lines.append("*No observation-level evidence comparisons recorded for this candidate.*")
            lines.append("")

    # 6. Complexity and Uncertainty
    lines.append("## 6. Complexity and Uncertainty")
    lines.append("")
    lines.append(f"- **Complexity Definition:** {cu.complexity_definition}")
    lines.append(f"- **Uncertainty Calibration:** {cu.uncertainty_status}")
    lines.append("Disclosures:")
    for disc in cu.uncertainty_disclosures:
        lines.append(f"- {disc}")
    lines.append("")

    # 7. Budget and Execution Summary
    lines.append("## 7. Budget & Resource Usage")
    lines.append("")
    lines.append("| Dimension | Configured Limit | Measured Consumption |")
    lines.append("|---|---|---|")
    max_q = be.configured_limits.get("max_queries", "Disabled")
    lines.append(f"| **Oracle Queries** | {max_q} | {be.measured_usage.get('queries_executed', 0)} |")
    max_e = be.configured_limits.get("max_evaluations", "Disabled")
    lines.append(f"| **Candidate Evaluations** | {max_e} | {be.measured_usage.get('evaluations_completed', 0)} |")
    lines.append("")

    # Cost breakdown
    cost = be.cost
    cost_state = cost.get("state", "unavailable")
    cost_usd = cost.get("estimated_cost_usd") if cost_state == "estimated" else cost.get("settled_cost_usd")
    cost_str = f"${cost_usd:.4f} USD" if cost_usd is not None else "Unavailable (provider cost not reported)"
    lines.append(f"**Compute Spend Classification:** `{cost_state.upper()}` — {cost_str}")
    if cost.get("pricing_assumptions"):
        lines.append(f"- *Pricing Assumptions:* {json.dumps(cost['pricing_assumptions'])}")
    lines.append("")

    # 8. Next Recommended Experiment
    lines.append("## 8. Next Experiment Recommendation")
    lines.append("")
    if ne.recommended:
        lines.append(f"**Recommended Query Input:** `{json.dumps(ne.input_values)}`")
        lines.append(f"- **Strategy:** `{ne.selection_strategy}`")
        if ne.predicted_disagreement is not None:
            lines.append(f"- **Predicted Disagreement:** {ne.predicted_disagreement:.4f}")
        lines.append(f"- **Justification:** {ne.justification}")
    else:
        lines.append(f"*No immediate resolving experiment recommended:* {ne.justification}")
    lines.append("")

    # 9. Limitations
    lines.append("## 9. Limitations & Caveats")
    lines.append("")
    if report.limitations:
        for lim in report.limitations:
            lines.append(f"- {lim}")
    else:
        lines.append("- No explicit limitations recorded.")
    lines.append("")

    # 10. Conclusion
    lines.append("## 10. Conclusion")
    lines.append("")
    lines.append(report.conclusion)
    lines.append("")

    return "\n".join(lines)
