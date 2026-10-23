# CLARITY Frontend

The researcher web interface for CLARITY, built with React and Vite. It reads
from the Flask API, renders run lifecycle and budget state, and exposes
hypotheses, scientific reports, and benchmarks without computing science in the
browser. See [docs/research-dashboard.md](../docs/research-dashboard.md) for the
page map, data states, and limitations.

## Setup and Development

1. **Install dependencies:**
   ```bash
   cd frontend
   npm ci
   ```

2. **Configure environment:**
   Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
   `VITE_API_BASE_URL` must include the API namespace, for example
   `http://127.0.0.1:5000/api/v1`. A bare origin sends requests to `/runs`
   instead of `/api/v1/runs`. The resolved URL is printed in the page footer.

3. **Start local development server:**
   ```bash
   npm run dev
   ```
   The application will run at `http://localhost:5173`. The Flask backend must
   already be running (see the root README).

## Automated Checks and Build

- **Tests:**
  ```bash
  npm test
  ```
- **Lint:**
  ```bash
  npm run lint
  ```
- **Production Build:**
  ```bash
  npm run build
  ```
