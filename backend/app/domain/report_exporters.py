"""Report exporters for CLARITY scientific discovery reports (Issue #23).

Supports:
- Structured machine-readable JSON export
- Formatted, human-readable scientific Markdown export
- Print-ready PDF export

Requirements:
- Escape user-provided text safely in Markdown and PDF.
- Ensure unavailable fields remain explicitly unavailable (e.g. "Unavailable" or null, never zero).
- Prevent export from exposing credentials, tokens, or ground-truth secrets.
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional, Sequence
from xml.sax.saxutils import escape as _xml_escape

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


# =============================================================================
# PDF export
# =============================================================================

# Style tokens mirror the interface's editorial identity: ink on warm paper,
# brass rules, serif headings. Colours are declared here so the export stays a
# pure function of the report with no application settings involved.

_PAPER = "#FBF8F2"
_INK = "#1C1714"
_INK_SOFT = "#403832"
_MUTED = "#6B5D50"
_BRASS = "#8C6A2F"
_RULE = "#CFC4B4"
_PARTIAL_TINT = "#F3E7D3"


def _pdf_units():
    """Import reportlab lazily so importing this module never needs it."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm

    return A4, mm


def _text(value: Any) -> str:
    """Escape a value for reportlab's Paragraph mini-markup."""
    if value is None or value == "":
        return "Unavailable"
    return _xml_escape(str(value)).replace("\n", "<br/>")


def _number(value: Optional[float], digits: int = 4) -> str:
    if value is None:
        return "Unavailable"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "Unavailable"


def _count(value: Optional[int]) -> str:
    return "Unavailable" if value is None else str(value)


def _rationale_fragments(explanation: str, hypothesis_id: str) -> List[str]:
    """Split the ranking engine's ledger into displayable lines.

    Fragments that repeat the candidate's own summary fields (display position,
    cohort rank, status, tier, complexity, evidence counts) are dropped so the
    PDF does not print the same fact twice.
    """
    if not explanation:
        return []
    fragments = [part.strip() for part in explanation.split(" | ") if part.strip()]
    kept: List[str] = []
    for fragment in fragments:
        lowered = fragment.lower()
        if fragment == hypothesis_id:
            continue
        if lowered.startswith(("status:", "tier:", "complexity:", "evidence:")):
            continue
        if lowered.startswith("within-cohort scientific rank"):
            continue
        # "Display position N" is repeated as a field; the sentence-bearing
        # "Display position in <tier> tier: ..." notes are kept.
        if lowered.startswith("display position ") and "tier:" not in lowered:
            continue
        kept.append(fragment)
    return kept


def export_report_as_pdf(report: ScientificReport) -> bytes:
    """Export the scientific report as a print-ready A4 PDF document.

    The layout follows the same section order as the Markdown export, and keeps
    the project's honesty rules: an unmeasured value is printed as
    "Unavailable", never as zero, and no confidence figure is invented.
    """
    # Imported here so the module can be imported without the optional renderer.
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        BaseDocTemplate,
        Frame,
        HRFlowable,
        KeepTogether,
        PageTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
    )

    A4, mm = _pdf_units()

    margin = 18 * mm
    content_width = A4[0] - 2 * margin

    def colour(hex_value: str):
        return colors.HexColor(hex_value)

    styles = {
        "eyebrow": ParagraphStyle(
            "eyebrow",
            fontName="Helvetica",
            fontSize=7,
            leading=10,
            textColor=colour(_BRASS),
            spaceAfter=6,
        ),
        "title": ParagraphStyle(
            "title",
            fontName="Times-Bold",
            fontSize=22,
            leading=26,
            textColor=colour(_INK),
            spaceAfter=4,
        ),
        "subtitle": ParagraphStyle(
            "subtitle",
            fontName="Helvetica",
            fontSize=7.5,
            leading=11,
            textColor=colour(_MUTED),
            spaceAfter=10,
        ),
        "h2": ParagraphStyle(
            "h2",
            fontName="Times-Bold",
            fontSize=13.5,
            leading=17,
            textColor=colour(_INK),
            spaceBefore=14,
            spaceAfter=6,
        ),
        "h3": ParagraphStyle(
            "h3",
            fontName="Times-Bold",
            fontSize=10.5,
            leading=14,
            textColor=colour(_INK),
            spaceBefore=10,
            spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "body",
            fontName="Times-Roman",
            fontSize=9.5,
            leading=13,
            textColor=colour(_INK_SOFT),
            alignment=TA_LEFT,
            spaceAfter=4,
        ),
        "label": ParagraphStyle(
            "label",
            fontName="Helvetica",
            fontSize=6.8,
            leading=9,
            textColor=colour(_MUTED),
        ),
        "value": ParagraphStyle(
            "value",
            fontName="Times-Roman",
            fontSize=9,
            leading=12,
            textColor=colour(_INK_SOFT),
        ),
        "cell": ParagraphStyle(
            "cell",
            fontName="Times-Roman",
            fontSize=8.2,
            leading=11,
            textColor=colour(_INK_SOFT),
        ),
        "cell_head": ParagraphStyle(
            "cell_head",
            fontName="Helvetica",
            fontSize=6.4,
            leading=9,
            textColor=colour(_MUTED),
        ),
        "cell_num": ParagraphStyle(
            "cell_num",
            fontName="Times-Roman",
            fontSize=8.2,
            leading=11,
            textColor=colour(_INK_SOFT),
            alignment=TA_RIGHT,
        ),
        "mono": ParagraphStyle(
            "mono",
            fontName="Courier",
            fontSize=7.6,
            leading=10,
            textColor=colour(_INK_SOFT),
        ),
        "partial": ParagraphStyle(
            "partial",
            fontName="Times-Italic",
            fontSize=9,
            leading=12.5,
            textColor=colour(_INK),
        ),
        "footer": ParagraphStyle(
            "footer",
            fontName="Helvetica",
            fontSize=6.4,
            leading=9,
            textColor=colour(_MUTED),
        ),
    }

    def paragraph(text: Any, style: str = "body", escape: bool = True) -> Paragraph:
        return Paragraph(_text(text) if escape else text, styles[style])

    def kv_table(rows: Sequence[Sequence[Any]], label_width: float = 150) -> Table:
        data = [
            [Paragraph(_text(label), styles["label"]), Paragraph(_text(value), styles["value"])]
            for label, value in rows
        ]
        table = Table(data, colWidths=[label_width, content_width - label_width])
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.25, colour(_RULE)),
                ]
            )
        )
        return table

    def grid_table(
        headers: Sequence[str],
        rows: Sequence[Sequence[Any]],
        widths: Sequence[float],
        numeric_columns: Sequence[int] = (),
    ) -> Table:
        data = [[Paragraph(_text(head), styles["cell_head"]) for head in headers]]
        for row in rows:
            data.append(
                [
                    cell
                    if isinstance(cell, Paragraph)
                    else Paragraph(
                        _text(cell),
                        styles["cell_num"] if index in numeric_columns else styles["cell"],
                    )
                    for index, cell in enumerate(row)
                ]
            )
        table = Table(data, colWidths=list(widths), repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.6, colour(_MUTED)),
                    ("LINEBELOW", (0, 1), (-1, -1), 0.25, colour(_RULE)),
                ]
            )
        )
        return table

    def rule(space_before: float = 6, space_after: float = 10) -> HRFlowable:
        return HRFlowable(
            width="100%",
            thickness=0.5,
            color=colour(_RULE),
            spaceBefore=space_before,
            spaceAfter=space_after,
        )

    def bullets(items: Sequence[str]) -> List[Paragraph]:
        return [paragraph(f"· {item}") for item in items if item]

    def evidence_table(items: Sequence[Any]) -> Table:
        shown = list(items)[:5]
        rows = [
            [
                Paragraph(_text(item.observation_id), styles["mono"]),
                paragraph("held-out" if item.is_held_out else "training"),
                paragraph(_number(item.observed_value)),
                paragraph(_number(item.predicted_value)),
                paragraph(_number(item.residual)),
                paragraph(_number(item.tolerance)),
            ]
            for item in shown
        ]
        table = grid_table(
            ["Observation", "Partition", "Observed", "Predicted", "Residual", "Tolerance"],
            rows,
            [118, 58, 62, 62, 62, content_width - 362],
            numeric_columns=(2, 3, 4, 5),
        )
        flowables: List[Any] = [table]
        if len(items) > len(shown):
            flowables.append(
                paragraph(
                    f"{len(items) - len(shown)} further observation(s) are recorded; "
                    "the full record is available in the JSON export."
                )
            )
        return KeepTogether(flowables)

    s = report.run_summary
    dp = report.discovery_process
    m = report.evaluation_methodology
    cu = report.complexity_and_uncertainty
    be = report.budget_and_execution
    ne = report.next_experiment

    story: List[Any] = []

    # Header block
    story.append(paragraph("CLARITY · SCIENTIFIC DISCOVERY REPORT", "eyebrow"))
    story.append(paragraph(s.name or "Scientific discovery report", "title"))
    story.append(
        paragraph(
            f"Run ID {report.run_id}   ·   Report ID {report.report_id}   ·   "
            f"Generated {report.generated_at}   ·   Schema {report.schema_version}",
            "subtitle",
        )
    )
    story.append(rule(0, 8))

    if s.is_partial:
        partial_rows = [
            [
                Paragraph(
                    "<b>Partial / incomplete run.</b> This run terminated before normal "
                    f"completion (status <b>{_text(s.status)}</b>, reason "
                    f"<i>{_text(s.stop_reason)}</i>). Reported candidates are intermediate "
                    "findings, not a completed discovery search.",
                    styles["partial"],
                )
            ]
        ]
        partial_box = Table(partial_rows, colWidths=[content_width])
        partial_box.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colour(_PARTIAL_TINT)),
                    ("LINEBEFORE", (0, 0), (0, -1), 2, colour(_BRASS)),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(partial_box)

    def section_flowables(title: str) -> List[Any]:
        return [
            Paragraph(_text(title), styles["h2"]),
            HRFlowable(
                width="100%",
                thickness=0.4,
                color=colour(_BRASS),
                spaceBefore=0,
                spaceAfter=8,
            ),
        ]

    def section(title: str) -> None:
        story.extend(section_flowables(title))

    # 1. Run summary
    section("1. Run summary")
    story.append(
        kv_table(
            [
                ("Domain", s.domain),
                ("Execution mode", s.execution_mode),
                ("Sampling strategy", s.sampling_strategy),
                ("Final status", s.status),
                ("Stop reason", s.stop_reason),
                ("Started at", s.started_at),
                ("Completed at", s.completed_at),
                ("Duration", f"{s.duration_seconds:.2f} s" if s.duration_seconds is not None else None),
            ]
        )
    )

    # 2. Discovery process
    section("2. Discovery process")
    story.append(paragraph(dp.search_progress))
    story.append(Spacer(1, 6))
    story.append(
        grid_table(
            ["Measure", "Count"],
            [
                ["Oracle queries executed", dp.oracle_queries_used],
                ["Candidate evaluations performed", dp.candidate_evaluations_performed],
                ["Hypotheses formulated", dp.hypotheses_generated],
            ],
            [content_width - 90, 90],
        )
    )

    # 3. Evaluation methodology
    section("3. Evaluation methodology")
    story.extend(
        bullets(
            [
                f"Primary metric: {m.primary_metric.upper()} — lower error indicates "
                "closer empirical agreement.",
                f"Held-out protocol: {m.held_out_evaluation_approach}",
                f"Dataset version: {_count(m.dataset_version)}",
            ]
        )
    )
    for name, definition in m.metric_definitions.items():
        story.append(paragraph(f"{name.upper()}: {definition}"))
    story.append(Spacer(1, 4))
    story.append(paragraph(f"Ranking criteria: {m.ranking_criteria}"))

    # 4. Ranked hypotheses
    if not report.ranked_hypotheses:
        section("4. Ranked hypotheses")
        story.append(
            paragraph(
                "No eligible candidate hypothesis was available for ranking in this run. "
                "No candidate has been invented to fill the table."
            )
        )
    else:
        ranked_intro = paragraph(
            "Rank is a display position for reading order only. The within-cohort rank "
            "inside each candidate block is the scientific ordering within a comparable "
            "evaluation cohort."
        )
        candidate_rows = [
            [
                str(h.rank),
                Paragraph(_text(h.hypothesis_id), styles["mono"]),
                h.model_family,
                Paragraph(_text(h.formula), styles["mono"]),
                _number(h.held_out_score),
                _number(h.training_score),
                h.status,
            ]
            for h in report.ranked_hypotheses
        ]
        ranked_table = grid_table(
            ["Display", "Hypothesis", "Family", "Expression", "Held out", "Training", "Status"],
            candidate_rows,
            [30, 92, 74, 120, 54, 54, content_width - 424],
            numeric_columns=(4, 5),
        )
        story.append(
            KeepTogether(
                section_flowables("4. Ranked hypotheses")
                + [ranked_intro, Spacer(1, 4), ranked_table]
            )
        )

        for h in report.ranked_hypotheses:
            block: List[Any] = [
                Paragraph(
                    _text(f"Candidate {h.rank} — {h.hypothesis_id}"),
                    styles["h3"],
                ),
                Paragraph(_text(h.formula), styles["mono"]),
                Spacer(1, 4),
            ]
            block.append(
                kv_table(
                    [
                        ("Within-cohort rank", _count(h.within_cohort_rank)),
                        ("Evaluation tier", h.evaluation_tier),
                        ("Plausibility status", h.status),
                        ("Dataset", h.dataset_id or "unknown"),
                        ("Complexity score", _count(h.complexity_score)),
                        (f"{h.primary_metric_name.upper()} held out", _number(h.held_out_score)),
                        (f"{h.primary_metric_name.upper()} training", _number(h.training_score)),
                        (
                            "Evidence",
                            f"{len(h.supporting_evidence)} supporting, "
                            f"{len(h.contradicting_evidence)} contradicting, "
                            f"{h.indeterminate_count} indeterminate",
                        ),
                    ],
                    label_width=120,
                )
            )
            rationale = _rationale_fragments(h.explanation, h.hypothesis_id)
            if rationale:
                block.append(Spacer(1, 6))
                block.append(Paragraph("Ranking rationale", styles["label"]))
                block.extend(bullets(rationale))
            story.append(KeepTogether(block))

            if h.contradicting_evidence:
                story.append(Spacer(1, 6))
                story.append(
                    Paragraph(
                        _text(f"Contradicting observations ({len(h.contradicting_evidence)})"),
                        styles["label"],
                    )
                )
                story.append(Spacer(1, 3))
                story.append(evidence_table(h.contradicting_evidence))
            if h.supporting_evidence:
                story.append(Spacer(1, 6))
                story.append(
                    Paragraph(
                        _text(f"Supporting observations ({len(h.supporting_evidence)})"),
                        styles["label"],
                    )
                )
                story.append(Spacer(1, 3))
                story.append(evidence_table(h.supporting_evidence))
            if not h.supporting_evidence and not h.contradicting_evidence:
                story.append(
                    paragraph("No observation-level evidence comparisons were recorded.")
                )

            if h.uncertainty_and_limitations:
                story.append(Spacer(1, 4))
                story.append(Paragraph("Recorded limitations", styles["label"]))
                story.extend(bullets(h.uncertainty_and_limitations))

    # 5. Complexity and uncertainty
    section("5. Complexity and uncertainty")
    story.append(paragraph(f"Complexity metric: {cu.complexity_metric}"))
    story.append(paragraph(cu.complexity_definition))
    story.append(paragraph(f"Uncertainty calibration: {cu.uncertainty_status}"))
    story.extend(bullets(cu.uncertainty_disclosures))

    # 6. Budget and execution
    cost = be.cost or {}
    cost_state = str(cost.get("state") or "unavailable")
    if cost_state == "settled":
        cost_value = cost.get("settled_cost_usd")
    elif cost_state == "accruing":
        cost_value = cost.get("accruing_cost_usd")
    else:
        cost_value = cost.get("estimated_cost_usd")
    budget_block: List[Any] = section_flowables("6. Budget and resource usage")
    budget_block.append(
        grid_table(
            ["Dimension", "Configured limit", "Measured consumption"],
            [
                [
                    "Oracle queries",
                    _text(be.configured_limits.get("max_queries")),
                    _count(be.measured_usage.get("queries_executed")),
                ],
                [
                    "Candidate evaluations",
                    _text(be.configured_limits.get("max_evaluations")),
                    _count(be.measured_usage.get("evaluations_completed")),
                ],
            ],
            [content_width - 240, 120, 120],
        )
    )
    budget_block.append(Spacer(1, 6))
    budget_block.append(
        paragraph(
            f"Compute spend classification: {cost_state.upper()} — "
            + (f"${float(cost_value):.4f} USD" if cost_value is not None else "amount unavailable")
        )
    )
    if cost.get("pricing_assumptions"):
        budget_block.append(
            paragraph(f"Pricing assumptions: {json.dumps(cost.get('pricing_assumptions'))}")
        )
    story.append(KeepTogether(budget_block))

    # 7. Next experiment
    section("7. Next experiment recommendation")
    if ne.recommended:
        story.append(
            paragraph(f"Recommended query input: {json.dumps(ne.input_values)}", "mono")
        )
        story.append(paragraph(f"Strategy: {ne.selection_strategy}"))
        if ne.predicted_disagreement is not None:
            story.append(
                paragraph(f"Predicted disagreement: {ne.predicted_disagreement:.4f}")
            )
    story.append(paragraph(ne.justification))

    # 8. Limitations
    section("8. Limitations and caveats")
    if report.limitations:
        story.extend(bullets(report.limitations))
    else:
        story.append(paragraph("No explicit limitations were recorded with this report."))

    # 9. Conclusion
    section("9. Conclusion")
    story.append(paragraph(report.conclusion))

    buffer = io.BytesIO()

    def decorate(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFillColor(colour(_PAPER))
        canvas.rect(0, 0, A4[0], A4[1], stroke=0, fill=1)
        canvas.setStrokeColor(colour(_RULE))
        canvas.setLineWidth(0.4)
        canvas.line(margin, margin - 6, A4[0] - margin, margin - 6)
        canvas.setFillColor(colour(_MUTED))
        canvas.setFont("Helvetica", 6.5)
        canvas.drawString(margin, margin - 16, f"CLARITY · report {report.report_id}")
        canvas.drawRightString(A4[0] - margin, margin - 16, f"Page {doc.page}")
        canvas.restoreState()

    document = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin + 10,
        title=f"Scientific discovery report — {s.name or report.run_id}",
        author="CLARITY",
        subject=f"Run {report.run_id} · report {report.report_id}",
        creator="CLARITY scientific report exporter",
    )
    frame = Frame(margin, margin + 6, content_width, A4[1] - 2 * margin - 6, id="body")
    document.addPageTemplates([PageTemplate(id="report", frames=[frame], onPage=decorate)])
    document.build(story)
    return buffer.getvalue()
