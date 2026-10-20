# CLARITY Research Dashboard (Frontend)

The researcher web interface for **CLARITY (Autonomous Black-Box Scientist)**, built with React and Vite.

## Architecture and Design Principles

- **Flask API Exclusivity:** The frontend communicates strictly via the REST API endpoints (`/api/runs`, `/api/health`, `/api/runs/{id}/*`). It does not directly access MySQL, the execution adapter, or internal discovery algorithms.
- **Scientific Honesty & Non-Fabrication:** Missing or unsettled values (e.g. compute cost during active execution or uncomputed error metrics) are displayed as *Unavailable*, never defaulted or fabricated as `$0.00` or zero error.
- **Bounded Polling:** Status and progress polling automatically activates when runs are in active execution (`running` or `pending`) and stops immediately when all runs reach terminal states (`completed`, `stopped_budget`, `failed`, `cancelled`).
- **Responsive & Accessible Glassmorphism:** Implements dark-mode glassmorphic styling, visible keyboard focus outlines, semantic ARIA roles, and distinct iconography that does not rely on color alone.

---

## Directory Structure

```text
frontend/
├── src/
│   ├── api/
│   │   ├── client.js          # Centralized Flask REST API client
│   │   └── client.test.js     # Unit tests for API client and error handling
│   ├── components/
│   │   ├── CommonStates.jsx   # Loading, Error, Empty, and StatusBadge components
│   │   ├── RunConfigModal.jsx # Validated discovery run creation form dialog
│   │   ├── RunDetailModal.jsx # Live run lifecycle, progress, budget & cost inspector
│   │   └── ScientificViewModal.jsx # Hypotheses, scientific report & benchmark views
│   ├── App.jsx                # Main research dashboard view
│   ├── App.css                # Polished dark glassmorphic styling
│   ├── index.css              # Global CSS tokens and variables
│   └── main.jsx               # React 19 application entrypoint
├── package.json
└── vite.config.js
```

---

## Setup and Local Development

### 1. Prerequisites
- **Node.js**: v20.x or later
- **npm**: v10.x or later
- Running backend at `http://127.0.0.1:5000` (see `backend/run.py`)

### 2. Install Dependencies
```bash
cd frontend
npm ci
```

### 3. Environment Configuration
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Default configuration:
```env
VITE_API_BASE_URL=http://127.0.0.1:5000
```

### 4. Run Development Server
```bash
npm run dev
```
The application will be accessible at `http://localhost:5173`.

---

## Verification and Automated Checks

```bash
# Code style and linting (oxlint)
npm run lint

# Production build bundle check
npm run build
```
