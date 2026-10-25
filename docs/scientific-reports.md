# Scientific Reports and Export Results (Issue #23)

CLARITY generates publication-ready, defensible scientific discovery reports for completed or stopped runs.
Reports summarize the discovery loop, rank candidate hypotheses using the established domain ranking engine, present supporting and contradicting evidence, disclose uncertainty, and suggest informative resolving experiments.

---

## 1. Principles & Scientific Honesty Invariants

1. **Defensible Ranking without Fabricated Confidence**:
   - Hypotheses are ranked deterministically by comparability cohort and primary metric error (e.g. minimizing held-out MAE) with complexity penalties.
   - Ranking indicates empirical agreement within tested domains, **never** subjective or uncalibrated probabilities such as "92% confidence".

2. **No Invented Candidates or Metrics**:
   - The report includes the top three to five valid hypotheses when available.
   - If fewer than three valid hypotheses exist, only available candidates are reported without inventing placeholders.
   - Missing metrics or unmeasured costs remain explicitly `null` (or "Unavailable") and are **never** represented as zero error or zero spend.

3. **Evidence & Provenance Tracking**:
   - Every reported metric traces back to a specific `EvaluationResultModel` and dataset version.
   - Supporting and contradicting observations reference real stored observations with empirical observed values, model predictions, and calculated residuals.

4. **Transparent Labeling of Incomplete Runs**:
   - Runs terminated due to budget exhaustion, manual cancellation, or execution failures produce clearly flagged partial reports disclosing that search space exploration was incomplete.

5. **Historical Reproducibility & Immutability**:
   - Report generation is idempotent. Generating a report persists a stable JSON artifact and database record.
   - Subsequent re-evaluations or hypothesis edits do not rewrite past historical reports.

---

## 2. Report Sections

Every scientific report contains 11 structured sections:

| Section | Key Contents |
|---|---|
| **1. Run Summary** | `run_id`, `name`, `domain`, `execution_mode`, `sampling_strategy`, `status`, `stop_reason`, `is_partial`, timestamps, duration. |
| **2. Discovery Process** | Oracle queries used, candidate evaluations performed, hypotheses generated, search progress description, and resource usage. |
| **3. Evaluation Methodology** | Primary metric (`mae`), metric definitions (MAE, RMSE, R²), held-out partitioning protocol, dataset version, and ranking criteria. |
| **4. Ranked Hypotheses** | Top 3–5 candidates with ranks, mathematical formulas, parameters, complexity, held-out scores, and scientific plausibility status. |
| **5. Evidence & Provenance** | Observation-level comparisons classified as supporting (|residual| ≤ tolerance) or contradicting (|residual| > tolerance), with partition indicators. |
| **6. Complexity & Uncertainty** | Model parameter count definitions and explicit disclosures regarding absence of Bayesian posterior distributions or calibrated intervals. |
| **7. Budget & Execution Summary** | Configured limits vs measured usage, with cost classified strictly as `estimated`, `accruing`, `settled`, or `unavailable`. |
| **8. Next Experiment** | Recommended candidate query from active disagreement planning to distinguish leading hypotheses, or justification when none is warranted. |
| **9. Limitations & Caveats** | Disclosures regarding missing held-out data, low sample sizes, domain extrapolation boundaries, and partial run termination. |
| **10. Conclusion** | Cautious scientific summary of what the data supports, what remains unresolved, and caveats regarding model approximations. |
| **11. Artifact Reference** | Relative URI to persisted immutable report JSON artifact. |

---

## 3. Supported Export Formats

### 3.1 JSON Export (`format=json` or `Accept: application/json`)
Machine-readable, indented JSON adhering to `REPORT_SCHEMA_VERSION = "1.0.0"`. Sensitive credentials, DSN passwords, and ground-truth secrets are scrubbed recursively.

### 3.2 Markdown Export (`format=markdown` or `Accept: text/markdown`)
Publication-ready Markdown with GitHub-flavored alerts, formatted tables, clean formulas, and safe table escaping.

### 3.3 PDF Export (`format=pdf` or `Accept: application/pdf`)
A4, print-ready PDF rendered server-side with ReportLab (`reportlab==4.2.5`). The document follows the same section order as the Markdown export and adds:
- the identical honesty rules: missing scores, datasets, or spend print as `Unavailable`, never as zero,
- a flagging box for partial or prematurely terminated runs,
- per-candidate blocks with within-cohort rank, evaluation tier, dataset, complexity, ranking rationale, and recorded limitations,
- observation-level evidence tables (first five per classification, with a note pointing to the JSON export for the full record),
- page footers carrying the report ID and page number.

The PDF is produced from the stored report record, so it is never assembled in the browser.

---

## 4. REST API Endpoints

### 4.1 Retrieve Report
```http
GET /api/v1/runs/{run_id}/report
```
- Returns `{"report": null, "status": "pending"}` if not yet generated.
- Returns `{"report": <serialized_report>}` once generated.

### 4.2 Generate Report
```http
POST /api/v1/runs/{run_id}/report
Content-Type: application/json

{
  "force_recompute": false
}
```
- Returns `201 Created` with the generated report.
- Idempotent: returns existing report unless `force_recompute: true` is passed.

### 4.3 Export Report
```http
GET /api/v1/runs/{run_id}/report/export?format=markdown
```
- Headers: `Content-Type: text/markdown; charset=utf-8`
- Returns raw formatted Markdown document.

```http
GET /api/v1/runs/{run_id}/report/export?format=json
```
- Headers: `Content-Type: application/json`
- Returns formatted JSON document.

```http
GET /api/v1/runs/{run_id}/report/export?format=pdf
```
- Headers: `Content-Type: application/pdf`, `Content-Disposition: attachment; filename="report_{run_id}.pdf"`
- Returns the rendered PDF document as a download.

Every export records a `REPORT_EXPORTED` audit event carrying the report ID and requested format.
