# CLARITY Frontend

The researcher web interface for CLARITY, built with React and Vite.

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
   By default, `VITE_API_BASE_URL` points to `http://127.0.0.1:5000`.

3. **Start local development server:**
   ```bash
   npm run dev
   ```
   The application will run at `http://localhost:5173`.

## Automated Checks and Build

- **Lint:**
  ```bash
  npm run lint
  ```
- **Production Build:**
  ```bash
  npm run build
  ```
