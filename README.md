# Wind Forecast Intelligence

A full-stack pilot dashboard for evaluating UK national wind generation forecasts against actual outturn during January 2026. The app uses only Elexon BMRS `WINDFOR` and `FUELHH` datasets, with React/Vite on the frontend and Python/FastAPI on the backend.

> **Important scope note:** This is a forecast-intelligence and accuracy-analysis pilot, not an automated trading recommendation. Demand, market prices, thermal availability, interconnector flows and other generation are outside the supplied scope, so the app does not calculate total residual demand or a buy quantity.

## Features

- Half-hourly January 2026 wind actuals and forecast-vintage comparison.
- Configurable prior-day UK-local publication cutoff; the selected forecast is the latest `publishTime` at or before that cutoff.
- MAE, RMSE, bias, MAPE (zero actuals excluded), WAPE and matched-interval coverage.
- Daily MAE view, delivery-date filter, and CSV export.
- Data-quality indicators and explicit source/scope notes.
- Docker Compose local development and health endpoint.

## Project layout

```text
wind-forecast-intelligence/
├── backend/
│   ├── app/main.py          # Elexon ingestion, vintage selection, metrics, FastAPI routes
│   ├── requirements.txt     # Python dependencies
│   └── Dockerfile           # Backend container
├── frontend/
│   ├── src/main.jsx         # React dashboard and charts
│   ├── src/style.css        # Responsive dashboard styles
│   ├── index.html           # Vite HTML entry point
│   ├── package.json         # Frontend dependencies and scripts
│   └── vite.config.js       # Vite configuration
├── docker-compose.yml       # Local frontend + backend orchestration
├── .gitignore
└── README.md
```

## Run locally with Docker

Requirements: Docker Desktop / Docker Engine with Compose.

```bash
docker compose up --build
```

Open the frontend at <http://localhost:5173>. Backend health: <http://localhost:8000/api/health>. API docs: <http://localhost:8000/docs>.

The backend requests Elexon data at runtime; the first analysis can take a little while because the requested period is fetched in seven-day windows. Internet access to Elexon's API is required. If the Elexon API is unavailable or returns a schema error, the UI displays an error rather than fabricating sample data.

## Run without Docker

Backend (Python 3.12 recommended):

```bash
cd backend
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Frontend (Node.js 20+):

```bash
cd frontend
npm install
npm run dev
```

By default, Vite calls `http://localhost:8000`. Set `VITE_API_URL` at build time if the backend is hosted elsewhere. Set `CORS_ORIGINS` on the backend to a comma-separated list of allowed frontend origins.

## Data sources and methodology

- Actual wind generation: `https://data.elexon.co.uk/bmrs/api/v1/datasets/FUELHH/stream`, filtered to `fuelType == WIND` and aggregated by `startTime`.
- Forecasts: `https://data.elexon.co.uk/bmrs/api/v1/datasets/WINDFOR/stream`.
- Delivery window: `2026-01-01T00:00:00Z` inclusive to `2026-02-01T00:00:00Z` exclusive.
- Timezone: `Europe/London` for decision cutoff and display; API timestamps are normalized to UTC for joins.
- Default decision cutoff: 10:00 UK local time on the day before delivery. This is an explicit pilot assumption, configurable from the UI, not a claim about the client's official trading process. Confirm the actual gate-closure / decision policy with the client before operational use.
- For every delivery interval, the selection algorithm chooses the eligible forecast with the greatest `publishTime` that is less than or equal to the cutoff. Later publications are excluded to avoid look-ahead leakage.
- Error convention: `forecast - actual`; positive bias indicates over-forecasting.
- MAE = mean absolute error; RMSE = square root of mean squared error; bias = mean signed error; MAPE excludes intervals with zero actual; WAPE = sum of absolute error / sum of absolute actual.
- Coverage reports the proportion of actual delivery intervals with an eligible forecast. Metrics use matched intervals only.

### Caveats to review

1. Confirm the client's exact day-ahead decision cutoff and whether it is fixed for all delivery periods.
2. Validate the current Elexon response schemas and ensure `FUELHH` WIND rows represent the intended national aggregate before using the output operationally.
3. API availability and historical retention may affect data completeness; the app exposes errors and coverage rather than silently filling missing values.
4. January 2026 is historical, so the dashboard is a retrospective analysis, not a current forecast service.
5. The app does not include load/demand forecasts, electricity prices, non-wind generation, network constraints or balancing costs.

## Deployment

The app is containerized. For a hosted demo, deploy the FastAPI backend as a Docker web service (for example, on Render or a container host) and the frontend as a static Vite site on Vercel. Configure `VITE_API_URL` to the public backend URL and `CORS_ORIGINS` to the Vercel domain. Vercel does not directly run the root `docker-compose.yml` as a multi-container deployment; the backend must be hosted separately. No live deployment URL is included until a deployment has actually been created and tested.

## AI tools disclosure

AI assistance (OpenAI ChatGPT) was used to help draft the initial application scaffold, frontend layout, API structure, data-analysis methodology and documentation. 
