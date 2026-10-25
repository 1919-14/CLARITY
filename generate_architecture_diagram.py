import os
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.patches import FancyBboxPatch

def create_clarity_architecture_diagram(output_path):
    # Dimensions: 32x22 inches at 160 DPI = 5120x3520 pixels (Razor-sharp 5K Ultra-HD)
    fig = plt.figure(figsize=(32, 22), facecolor='#0B0F19', dpi=160)
    ax = fig.add_axes([0, 0, 1, 1], facecolor='#0B0F19')
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis('off')

    # Color Palette - Modern Cyberpunk / Dark Engineering Studio
    BG_DARK      = '#0B0F19'
    CARD_BG      = '#111827'
    CARD_BORDER  = '#1F2937'
    TEXT_WHITE   = '#F8FAFC'
    TEXT_MUTED   = '#94A3B8'
    TEXT_DIM     = '#64748B'

    # Vibrant Accent Colors by Subsystem
    ACCENT_FRONTEND  = '#38BDF8'   # Sky Blue / React
    ACCENT_GATEWAY   = '#818CF8'   # Indigo / API
    ACCENT_SERVICE   = '#34D399'   # Emerald Green / Workers
    ACCENT_DISCOVERY = '#F59E0B'   # Amber / Active Learning Core
    ACCENT_ORACLE    = '#F43F5E'   # Rose / Coral / Blackbox
    ACCENT_PERSIST   = '#06B6D4'   # Cyan / Storage
    ACCENT_LLM       = '#C084FC'   # Violet / Groq LLM
    ACCENT_FLOW      = '#FCD34D'   # Warm Gold / Execution

    # Helper: Draw Outer Section Card
    def draw_section_card(x, y, w, h, title, subtitle="", accent=ACCENT_GATEWAY, bg=CARD_BG):
        # Outer glow border
        glow = FancyBboxPatch((x-0.15, y-0.15), w+0.3, h+0.3,
                              boxstyle="round,pad=0.4,rounding_size=0.6",
                              fc=accent, ec='none', alpha=0.12, zorder=2)
        ax.add_patch(glow)

        # Card container
        box = FancyBboxPatch((x, y), w, h,
                             boxstyle="round,pad=0.4,rounding_size=0.6",
                             fc=bg, ec=accent, lw=1.6, alpha=0.96, zorder=3)
        ax.add_patch(box)

        # Top Accent stripe
        stripe = FancyBboxPatch((x+0.5, y+h-0.35), w-1.0, 0.25,
                                boxstyle="round,pad=0.08,rounding_size=0.15",
                                fc=accent, ec='none', zorder=4)
        ax.add_patch(stripe)

        # Title & Subtitle with generous spacing
        ax.text(x + 1.2, y + h - 1.35, title, fontsize=12.5, fontweight='bold',
                color=TEXT_WHITE, va='center', zorder=5)
        if subtitle:
            ax.text(x + 1.2, y + h - 2.5, subtitle, fontsize=9.0,
                    color=TEXT_MUTED, va='center', zorder=5)

    # Helper: Draw Sub-module Component
    def draw_module(x, y, w, h, title, lines=None, tag="", border=CARD_BORDER, bg='#172033', title_color=TEXT_WHITE):
        m_box = FancyBboxPatch((x, y), w, h,
                               boxstyle="round,pad=0.25,rounding_size=0.35",
                               fc=bg, ec=border, lw=1.2, zorder=5)
        ax.add_patch(m_box)

        ax.text(x + 0.8, y + h - 1.1, title, fontsize=10.0, fontweight='bold',
                color=title_color, va='center', zorder=6)

        if tag:
            t_box = FancyBboxPatch((x + w - 3.8, y + h - 1.55), 3.0, 0.9,
                                   boxstyle="round,pad=0.1,rounding_size=0.2",
                                   fc='#0F172A', ec=border, lw=0.9, zorder=6)
            ax.add_patch(t_box)
            ax.text(x + w - 2.3, y + h - 1.1, tag, fontsize=7.2, fontweight='bold',
                    color=border, va='center', ha='center', zorder=7)

        if lines:
            line_y = y + h - 2.2
            for line in lines:
                ax.text(x + 0.8, line_y, line, fontsize=8.0, color=TEXT_MUTED, va='center', zorder=6)
                line_y -= 0.95

    # Helper: Draw Directional Arrow with Clean Badge
    def draw_arrow(x1, y1, x2, y2, label="", color=ACCENT_GATEWAY, rad=0.0,
                   badge_pos=0.5, offset_y=0.0, offset_x=0.0, lw=2.0, style='->'):
        conn = f"arc3,rad={rad}"
        ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                    arrowprops=dict(arrowstyle=style, color=color, lw=lw,
                                    shrinkA=4, shrinkB=4,
                                    connectionstyle=conn),
                    zorder=8)
        if label:
            mx = x1 + (x2 - x1) * badge_pos + offset_x
            my = y1 + (y2 - y1) * badge_pos + offset_y
            ax.text(mx, my, label, fontsize=8.0, fontweight='bold',
                    color=color, ha='center', va='center',
                    bbox=dict(boxstyle="round,pad=0.35", fc='#070B14', ec=color, lw=1.1, alpha=0.96),
                    zorder=10)

    # =========================================================================
    # 1. TOP HEADER & METADATA BAR (y: 93.5 to 98.7)
    # =========================================================================
    draw_section_card(2.5, 93.5, 95, 5.2,
                      "CLARITY — Autonomous Black-Box Scientific Discovery Platform",
                      "End-to-End System Architecture, Modular Component Topology & Active-Learning Execution Flow",
                      accent='#38BDF8', bg='#0F172A')

    # System badges on right
    ax.text(95.5, 96.6, "SYSTEM SPECIFICATION: V2.1 PRODUCTION READY", fontsize=9.2, fontweight='bold',
            color='#38BDF8', ha='right', zorder=6)
    ax.text(95.5, 95.1, "TECH STACK: React 19 • Vite • Flask • SQLAlchemy • Active Learning • Groq Llama 3",
            fontsize=8.5, color=TEXT_MUTED, ha='right', zorder=6)

    # =========================================================================
    # 2. CLIENT LAYER (Frontend SPA) (y: 77.0 to 91.8, h: 14.8)
    # =========================================================================
    draw_section_card(2.5, 77.0, 95, 14.8,
                      "1. CLIENT PRESENTATION LAYER (React 19 + Vite SPA — Port 5173)",
                      "Responsive Web Application with Glassmorphic Theme, Real-time Polling & Live Telemetry",
                      accent=ACCENT_FRONTEND)

    draw_module(4.0, 78.2, 17.5, 9.8,
                "Oracle Library Page",
                ["• Upload Blackbox Models",
                 "• Formats: .py, .joblib, .onnx",
                 "• Automated Schema Probing",
                 "• Two-Phase Force-Deletion Flow"],
                tag="PORTAL", border=ACCENT_FRONTEND, title_color='#E0F2FE')

    draw_module(23.0, 78.2, 17.5, 9.8,
                "Create Run Page",
                ["• Search Space & Domain Bounds",
                 "• Hard Query / Time Budgets",
                 "• Strategy Selection (Active/Rand)",
                 "• Groq LLM Scientist Toggle"],
                tag="WIZARD", border=ACCENT_FRONTEND, title_color='#E0F2FE')

    draw_module(42.0, 78.2, 17.5, 9.8,
                "Run Details & Monitor",
                ["• Real-time Polling (1.5s Interval)",
                 "• 2D/3D Observation Scatter Plot",
                 "• Dynamic Residual Curve Graph",
                 "• Status & Resource Counters"],
                tag="TELEMETRY", border=ACCENT_FRONTEND, title_color='#E0F2FE')

    draw_module(61.0, 78.2, 17.5, 9.8,
                "Hypotheses Explorer",
                ["• Mathematical Equation Renderer",
                 "• Empirical Fit (MSE & R² Score)",
                 "• Complexity & Simplicity Metric",
                 "• Multi-Objective Pareto Frontier"],
                tag="DISCOVERY", border=ACCENT_FRONTEND, title_color='#E0F2FE')

    draw_module(80.0, 78.2, 16.0, 9.8,
                "Benchmarks & Reports",
                ["• Damped Oscillator / Reactor",
                 "• Ground-Truth Verification",
                 "• Export Markdown / PDF / JSON",
                 "• Reproducibility Run Bundles"],
                tag="REPORTS", border=ACCENT_FRONTEND, title_color='#E0F2FE')

    # =========================================================================
    # 3. API GATEWAY & ROUTERS (Flask REST API) (y: 63.5 to 74.7, h: 11.2)
    # =========================================================================
    draw_section_card(2.5, 63.5, 95, 11.2,
                      "2. API GATEWAY & CONTROLLERS (Flask REST API — Port 5000 /api/v1)",
                      "CORS-Enabled Blueprint Endpoints, Request Validation, Asynchronous Worker Dispatch & Security Guards",
                      accent=ACCENT_GATEWAY)

    draw_module(4.0, 64.7, 14.5, 6.6,
                "/api/v1/oracles",
                ["• Multi-part upload handler",
                 "• Sandbox probing trigger",
                 "• Run-binding cascade check"],
                tag="ROUTE", border=ACCENT_GATEWAY)

    draw_module(20.0, 64.7, 14.5, 6.6,
                "/api/v1/runs",
                ["• Create & configure runs",
                 "• Async lifecycle start/stop",
                 "• Live telemetry polling"],
                tag="ROUTE", border=ACCENT_GATEWAY)

    draw_module(36.0, 64.7, 14.5, 6.6,
                "/api/v1/hypotheses",
                ["• Fetch candidate equations",
                 "• Query Pareto rankings",
                 "• Residual breakdown"],
                tag="ROUTE", border=ACCENT_GATEWAY)

    draw_module(52.0, 64.7, 14.5, 6.6,
                "/api/v1/observations",
                ["• Input queries vector X",
                 "• Measured blackbox response Y",
                 "• Step index tracking"],
                tag="ROUTE", border=ACCENT_GATEWAY)

    draw_module(68.0, 64.7, 14.0, 6.6,
                "/api/v1/benchmarks",
                ["• Classical ODE systems",
                 "• Baseline comparison suite",
                 "• Error rate & query counts"],
                tag="ROUTE", border=ACCENT_GATEWAY)

    draw_module(83.5, 64.7, 12.5, 6.6,
                "/api/v1/reports",
                ["• LaTeX formula export",
                 "• Markdown synthesis",
                 "• PDF download stream"],
                tag="ROUTE", border=ACCENT_GATEWAY)

    # =========================================================================
    # 4. MIDDLE TIER: SERVICES (Left) + DISCOVERY CORE (Center) + ORACLES (Right)
    # =========================================================================

    # 4A. SERVICE LAYER (x: 2.5, y: 34.0, w: 29.5, h: 27)
    draw_section_card(2.5, 34.0, 29.5, 27.0,
                      "3. APPLICATION SERVICES & RUNTIME",
                      "Execution Threads, Admission & Lifecycle",
                      accent=ACCENT_SERVICE)

    draw_module(4.0, 52.5, 26.5, 5.0,
                "OracleService & Security Sandbox",
                ["• Dynamic loader for Python/Joblib/ONNX",
                 "• AST security checks & input sanitization"],
                tag="SANDBOX", border=ACCENT_SERVICE, title_color='#A7F3D0')

    draw_module(4.0, 46.5, 26.5, 5.0,
                "RunExecutionService & Worker Thread",
                ["• Spawns dedicated daemon thread per run",
                 "• Non-blocking async loop with stop-events"],
                tag="THREADING", border=ACCENT_SERVICE, title_color='#A7F3D0')

    draw_module(4.0, 40.5, 26.5, 5.0,
                "RunOrchestrator & EngineFactory",
                ["• Wires active exploration strategies",
                 "• Drives iterative sampling & termination checks"],
                tag="ORCHESTRATOR", border=ACCENT_SERVICE, title_color='#A7F3D0')

    draw_module(4.0, 35.2, 26.5, 4.3,
                "BudgetAdmission & Resource Service",
                ["• Max oracle query enforcement",
                 "• Wall-clock execution timeout protection"],
                tag="ADMISSION", border=ACCENT_SERVICE, title_color='#A7F3D0')

    # 4B. SCIENTIFIC DISCOVERY CORE (x: 34.0, y: 34.0, w: 35.5, h: 27)
    draw_section_card(34.0, 34.0, 35.5, 27.0,
                      "4. SCIENTIFIC DISCOVERY ENGINE (Domain Core)",
                      "Active Learning, Symbolic Regression & AI Hypothesis Generation",
                      accent=ACCENT_DISCOVERY)

    draw_module(35.5, 53.0, 32.5, 4.6,
                "A. Candidate Experiment Generator",
                ["• Latin Hypercube & Sobol sampling in search space",
                 "• Boundary & interior domain partition"],
                tag="SAMPLING", border=ACCENT_DISCOVERY, title_color='#FDE68A')

    draw_module(35.5, 47.0, 32.5, 4.8,
                "B. Active Selection Engine (Bayesian Acquisition)",
                ["• Epistemic uncertainty maximization & Info-Gain",
                 "• Exploitation vs exploration balance"],
                tag="SELECTION", border=ACCENT_DISCOVERY, title_color='#FDE68A')

    draw_module(35.5, 41.0, 32.5, 4.8,
                "C. Fitting Engine & Symbolic Regression",
                ["• Nonlinear least-squares optimization (BFGS / Levenberg)",
                 "• AST symbolic tree mutation & syntax validation"],
                tag="REGRESSION", border=ACCENT_DISCOVERY, title_color='#FDE68A')

    # Re-arranged: Ranking Engine on left, LLM Scientist on right
    # so LLM Scientist has direct line of sight to Groq API on right!
    draw_module(35.5, 35.2, 15.5, 4.6,
                "D. Ranking Engine",
                ["• Pareto frontier sorting",
                 "• MSE vs Complexity balance",
                 "• Generalization penalty"],
                tag="PARETO", border=ACCENT_DISCOVERY, title_color='#FDE68A')

    draw_module(52.5, 35.2, 15.5, 4.6,
                "E. LLM Scientist",
                ["• Physical law induction",
                 "• Mechanism conjecture",
                 "• Guided by Groq API"],
                tag="AI AGENT", border=ACCENT_LLM, title_color='#DDD6FE')

    # 4C. ORACLES & SYSTEM UNDER TEST (x: 71.5, y: 34.0, w: 26.0, h: 27)
    draw_section_card(71.5, 34.0, 26.0, 27.0,
                      "5. ORACLES & BLACK-BOX ADAPTERS",
                      "Pluggable Target Systems Under Discovery",
                      accent=ACCENT_ORACLE)

    draw_module(73.0, 52.8, 23.0, 4.8,
                "Python Script Oracle (.py)",
                ["• Arbitrary dynamic ODE functions `run(x)`",
                 "• e.g. Damped Harmonic Oscillator"],
                tag="DYNAMIC", border=ACCENT_ORACLE, title_color='#FECDD3')

    draw_module(73.0, 47.0, 23.0, 4.8,
                "ML Model Oracle (.joblib)",
                ["• Scikit-Learn pipelines & Random Forests",
                 "• Serialized regressors / classifiers"],
                tag="SKLEARN", border=ACCENT_ORACLE, title_color='#FECDD3')

    draw_module(73.0, 41.2, 23.0, 4.8,
                "Deep Learning Oracle (.onnx)",
                ["• ONNX Runtime accelerated execution",
                 "• PyTorch / TensorFlow exported graphs"],
                tag="ONNX", border=ACCENT_ORACLE, title_color='#FECDD3')

    draw_module(73.0, 35.4, 23.0, 4.8,
                "Analytical Benchmark Simulators",
                ["• Built-in Catalytic Reactor & Oscillators",
                 "• Exact mathematical ground truth"],
                tag="BENCHMARK", border=ACCENT_ORACLE, title_color='#FECDD3')

    # =========================================================================
    # 5. BOTTOM TIER: PERSISTENCE (Left) + EXTERNAL AI (Right)
    # =========================================================================

    # 5A. PERSISTENCE LAYER (x: 2.5, y: 17.5, w: 67.0, h: 14.5)
    draw_section_card(2.5, 17.5, 67.0, 14.5,
                      "6. PERSISTENCE & STORAGE LAYER (SQLAlchemy ORM + Local Filesystem)",
                      "Relational SQLite / MySQL Database with ACID guarantees & Local Disk Model Artifact Repository",
                      accent=ACCENT_PERSIST)

    draw_module(4.0, 18.8, 12.0, 9.8,
                "Runs & Status",
                ["• run_id (UUID)",
                 "• status: CREATED,",
                 "  RUNNING, DONE",
                 "• budget_limits",
                 "• oracle_id (FK)",
                 "• strategy_type"],
                tag="TABLE", border=ACCENT_PERSIST)

    draw_module(17.2, 18.8, 12.0, 9.8,
                "Oracle Artifacts",
                ["• oracle_id (PK)",
                 "• artifact_type",
                 "• input_schema",
                 "• output_schema",
                 "• sha256_hash",
                 "• disk_filepath"],
                tag="TABLE", border=ACCENT_PERSIST)

    draw_module(30.4, 18.8, 12.0, 9.8,
                "Observations",
                ["• observation_id",
                 "• run_id (FK)",
                 "• step_number",
                 "• vector_x (JSON)",
                 "• value_y (Float)",
                 "• probe_latency"],
                tag="TABLE", border=ACCENT_PERSIST)

    draw_module(43.6, 18.8, 12.0, 9.8,
                "Hypotheses",
                ["• hypothesis_id",
                 "• run_id (FK)",
                 "• formula_str",
                 "• mse_error",
                 "• r2_score",
                 "• pareto_rank"],
                tag="TABLE", border=ACCENT_PERSIST)

    draw_module(56.8, 18.8, 11.5, 9.8,
                "Audit & Reports",
                ["• audit_events",
                 "• budget_reservations",
                 "• generated_reports",
                 "• benchmark_results",
                 "• latex_summaries"],
                tag="TABLE", border=ACCENT_PERSIST)

    # 5B. EXTERNAL AI PROVIDER (x: 71.5, y: 17.5, w: 26.0, h: 14.5)
    draw_section_card(71.5, 17.5, 26.0, 14.5,
                      "7. EXTERNAL AI REASONING",
                      "Cloud LLM Engine for Physical Laws",
                      accent=ACCENT_LLM)

    draw_module(73.0, 18.8, 23.0, 9.8,
                "Groq LLM Cloud API (GroqProvider)",
                ["• Model: Llama 3 / Mixtral-8x7b",
                 "• Ultra-fast token generation",
                 "• Translates residuals into plausible physical laws",
                 "• Suggests non-linear basis functions",
                 "• Scientific reasoning & mechanism insights"],
                tag="CLOUD API", border=ACCENT_LLM, title_color='#E9D5FF')

    # =========================================================================
    # 6. STEP-BY-STEP DETAILED FLOW PIPELINE (Bottom Box: y: 1.5 to 15.5)
    # =========================================================================
    draw_section_card(2.5, 1.5, 95.0, 14.5,
                      "CLARITY EXECUTION FLOW & DATA LIFECYCLE (Step-by-Step)",
                      "Chronological flow through the active scientific discovery pipeline from black-box upload to scientific law delivery",
                      accent=ACCENT_FLOW, bg='#0E1626')

    pipeline_steps = [
        ("Step 1: Oracle Onboarding",
         "User uploads black-box (.py, .joblib, .onnx) via OracleLibraryPage. OracleService sandboxes file, probes input/output dimensions, computes SHA256, and stores record in oracle_artifacts table.",
         ACCENT_ORACLE),

        ("Step 2: Run Configuration",
         "User defines parameter search space, iteration budget (e.g. 50 queries) & strategy in CreateRunPage. API validates payload, reserves budget quota, and creates RunModel record (status=CREATED).",
         ACCENT_FRONTEND),

        ("Step 3: Async Worker Dispatch",
         "Client calls POST /runs/{id}/start. RunExecutionService spawns an isolated background daemon thread, instantiating DiscoveryEngine and switching run state to RUNNING.",
         ACCENT_SERVICE),

        ("Step 4: Active Probing Loop",
         "At each step: (1) Generator proposes candidate points; (2) Selection Engine picks highest-uncertainty query; (3) Oracle Adapter queries blackbox; (4) (x, y) Observation is committed to database.",
         ACCENT_DISCOVERY),

        ("Step 5: Hypothesis & AI Induction",
         "FittingEngine optimizes parameters via symbolic regression. Groq LLM Scientist interprets residual patterns to hypothesize physical equations. RankingEngine updates Pareto frontier.",
         ACCENT_LLM),

        ("Step 6: Telemetry & Synthesis",
         "Frontend continuously polls /runs/{id} every 1.5s to plot live scatter telemetry and top equations. Upon budget completion, ReportService generates comprehensive markdown & LaTeX reports.",
         ACCENT_PERSIST)
    ]

    card_width = 15.1
    spacing = 15.9
    for i, (stitle, sdesc, scolor) in enumerate(pipeline_steps):
        cx = 3.6 + i * spacing
        cy = 2.4

        step_box = FancyBboxPatch((cx, cy), card_width, 10.3,
                                  boxstyle="round,pad=0.2,rounding_size=0.35",
                                  fc='#151F33', ec=scolor, lw=1.2, zorder=5)
        ax.add_patch(step_box)

        h_box = FancyBboxPatch((cx + 0.5, cy + 8.6), card_width - 1.0, 1.2,
                               boxstyle="round,pad=0.1,rounding_size=0.2",
                               fc='#0B1120', ec=scolor, lw=0.9, zorder=6)
        ax.add_patch(h_box)

        ax.text(cx + card_width/2, cy + 9.2, stitle, fontsize=8.6, fontweight='bold',
                color=scolor, ha='center', va='center', zorder=7)

        words = sdesc.split()
        wrapped_lines = []
        cur_line = []
        for w in words:
            cur_line.append(w)
            if len(" ".join(cur_line)) > 24:
                wrapped_lines.append(" ".join(cur_line))
                cur_line = []
        if cur_line:
            wrapped_lines.append(" ".join(cur_line))

        ty = cy + 7.8
        for line in wrapped_lines[:8]:
            ax.text(cx + 0.6, ty, line, fontsize=7.6, color=TEXT_MUTED, va='top', zorder=6)
            ty -= 0.85

    # =========================================================================
    # 7. METICULOUSLY POSITIONED FLOW ARROWS (Zero overlap guarantee)
    # =========================================================================

    # Arrow 1: Frontend Oracle Upload -> /api/v1/oracles
    draw_arrow(12.5, 78.2, 11.2, 71.3,
               label="[1] Upload & Probe Blackbox", color=ACCENT_FRONTEND,
               badge_pos=0.5, offset_x=0.0, offset_y=0.0, lw=2.0)

    # Arrow 2: Frontend Create Run -> /api/v1/runs
    draw_arrow(31.5, 78.2, 27.2, 71.3,
               label="[2] POST /runs (Config & Budget)", color=ACCENT_FRONTEND,
               badge_pos=0.5, offset_x=0.0, offset_y=0.0, lw=2.0)

    # Arrow 3: Frontend Polling Run Details -> /api/v1/runs
    draw_arrow(50.5, 78.2, 28.5, 71.3,
               label="[6] Live Polling (1.5s Telemetry)", color=ACCENT_FRONTEND,
               badge_pos=0.45, offset_x=0.0, offset_y=1.0, rad=0.12, lw=2.0)

    # Arrow 4: /api/v1/oracles -> OracleService
    draw_arrow(11.2, 64.7, 11.2, 57.5,
               label="Validate & Probe Schema", color=ACCENT_GATEWAY,
               badge_pos=0.5, offset_x=0.0, offset_y=0.0, lw=1.8)

    # Arrow 5: /api/v1/runs -> RunExecutionService
    draw_arrow(25.0, 64.7, 21.0, 51.5,
               label="[3] Dispatch Async Worker", color=ACCENT_SERVICE,
               badge_pos=0.5, offset_x=0.0, offset_y=0.0, lw=2.2)

    # Arrow 6: RunOrchestrator -> DiscoveryEngine
    draw_arrow(30.5, 43.0, 35.5, 43.0,
               label="[4a] Execute Active Loop", color=ACCENT_DISCOVERY,
               badge_pos=0.5, offset_x=0.0, offset_y=1.1, lw=2.4)

    # Arrow 7: Discovery Engine -> Oracle Adapter (Outbound Query X)
    draw_arrow(68.0, 49.5, 73.0, 49.5,
               label="[4b] Probe Query X", color=ACCENT_ORACLE,
               badge_pos=0.5, offset_x=0.0, offset_y=1.1, lw=2.4)

    # Arrow 8: Oracle Adapter -> Discovery Engine (Inbound Response Y)
    draw_arrow(73.0, 45.0, 68.0, 45.0,
               label="Blackbox Response Y", color=ACCENT_ORACLE,
               badge_pos=0.5, offset_x=0.0, offset_y=-1.1, lw=2.4)

    # Arrow 9: LLM Scientist -> Groq API (Request - right edge of LLM Scientist to top edge of Groq API)
    draw_arrow(68.0, 37.0, 75.0, 28.6,
               label="[5a] Prompt Residuals & Context", color=ACCENT_LLM,
               badge_pos=0.5, offset_x=0.5, offset_y=1.1, lw=2.2)

    # Arrow 10: Groq API -> LLM Scientist (Response - left edge of Groq API to bottom edge of LLM Scientist)
    draw_arrow(73.0, 24.5, 62.0, 35.2,
               label="[5b] Inducted Laws & Formulas", color=ACCENT_LLM,
               badge_pos=0.5, offset_x=-0.5, offset_y=-1.1, lw=2.2)

    # Arrow 11: Discovery Engine -> Persistence (Straight clean drop down into Hypotheses/Observations)
    draw_arrow(43.0, 34.0, 43.0, 28.6,
               label="Commit Observations & Hypotheses", color=ACCENT_PERSIST,
               badge_pos=0.5, offset_x=0.0, offset_y=0.0, lw=2.2)

    # Arrow 12: RunExecutionService -> Persistence (Straight clean drop down into Runs)
    draw_arrow(10.0, 34.0, 10.0, 28.6,
               label="Update Run State (DONE)", color=ACCENT_PERSIST,
               badge_pos=0.5, offset_x=0.0, offset_y=0.0, lw=2.0)

    # Save High-Resolution Diagram
    plt.savefig(output_path, dpi=160, facecolor=BG_DARK, edgecolor='none', bbox_inches='tight')
    plt.close()
    print(f"[SUCCESS] High-res diagram rendered to {output_path}")

if __name__ == "__main__":
    out_file = r"c:\Users\saina\Videos\CLARITY\CLARITY_Architecture_Diagram.png"
    create_clarity_architecture_diagram(out_file)
