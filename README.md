# AI Models Tracker

AI Models Tracker is an interactive catalog for AI models, pricing, capabilities, provider news, and retirement dates. Search and filter models, compare capabilities and costs, and follow changes from configured sources. A FastAPI backend serves the browser UI and refreshes the JSON datasets in `data/`.

## Requirements

- Python **3.12** (the project intentionally requires this exact minor version).
- The recommended Render setup uses its native Python runtime; Docker is not required.

## Run locally on Windows

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m backend.jobs all
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload
```

Open <http://localhost:8000>. The API health check is at `/api/health`; API docs are at `/api/docs`.
The app opens on the model catalog. Open a model to view its full available description, pricing, limits, capabilities, retirement data, and source links. When a source has no narrative description, the profile clearly labels its metadata-based summary instead.

Model records, prices, capabilities, and retirement dates are collected from the configured upstream APIs and provider pages; they are not maintained as model entries in the application code. The JSON files under `data/` are the last successful collection cache, not the source of truth. Run `python -m backend.jobs all` before starting locally for a fresh collection. In production, the admin-configured scheduler refreshes the data every six hours by default. The catalog page displays the cache's last refresh time and the source identifiers represented in the collected records.

Fetch jobs can also be run separately:

```powershell
python -m backend.jobs prices
python -m backend.jobs deprecations
python -m backend.jobs news
python -m backend.jobs icons
```

Configure `user_agent` in `sources.yaml` with a contact address and project URL before running the collectors.

## Project layout

```text
backend/
  app.py          FastAPI application and read-only data API
  jobs.py         Data collectors and refresh scheduler
frontend/
  index.html      Responsive browser application
data/             JSON datasets and provider icons
tests/            API tests
sources.yaml      Provider, feed, and purpose configuration
```

## Production deployment

### Render native Python (no Docker)

Create a Render **Web Service** with the **Python** runtime and Python 3.12. Use:

- Build command: `python -m pip install .`
- Start command: `sh ./start.sh`
- Health check path: `/api/health`
- Persistent disk mount path: `/opt/render/project/src/data`

`start.sh` fetches data if the persistent data directory is empty, then starts Uvicorn on Render's `PORT` (default `10000`). Attach a persistent disk and run exactly one application instance. Render persistent disks require a paid service; without a disk, refreshed JSON datasets and admin settings are lost on restarts or deploys.

Configure these variables in the host's environment settings:

- `ADMIN_USERNAME`: the admin account name.
- `ADMIN_PASSWORD`: a strong, unique admin password.
- `ADMIN_SESSION_SECRET`: a random secret of at least 32 bytes (for example, generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`).
- `ADMIN_COOKIE_SECURE`: leave unset or set to `true` in production. Set it to `false` only for local HTTP development.

Open `/admin/login` to sign in; the admin dashboard is at `/admin`. The admin page and every admin API require a valid, HTTP-only signed session; unauthenticated visitors are redirected to the login form and cannot load the admin page. The admin can trigger a refresh immediately or select 15, 30, or 45 minutes, or a whole-hour interval from 1 to 24 hours. The default for a new settings file is six hours. After the initial data load, scheduled refreshes run at the configured interval; use **Refresh now** for an immediate run. The scheduler starts only when all three admin environment variables are configured. Existing saved settings are preserved across deployments, and legacy hour-based settings are migrated automatically. The scheduler runs inside the web process; use one instance/worker to avoid duplicate schedules. Provider icons are refreshed no more than once per day.

The GitHub Actions workflow in `.github/workflows/fetch.yml` is manual-only; it no longer competes with the admin-configured scheduler. GitHub Pages cannot host the Python API.

The public API exposes only the named datasets under `/api/data/{dataset}`. Admin credentials are configured only through deployment environment secrets and are never stored in the repository. If a data file is unavailable, the API reports an explicit service error instead of returning an invalid response.

## Data sources and limits

- **Models and prices**: OpenRouter Models API and all providers/resellers in models.dev, merged by normalized model name; Qwen also has direct Alibaba Cloud Model Studio coverage.
- **Model coverage and browsing**: coverage is limited to records returned by those configured sources; it is not a guarantee of every model available from every provider. The catalog loads 100 matching models at a time. “Best for” shows up to ten rule-based matches per purpose, not benchmark rankings.
- **Qwen catalog and retirements**: Alibaba Cloud Model Studio's recommended-model catalog and Token Plan documentation. The collector stores models listed there and calculates 24:00 preview end dates as the following retirement date.
- **Changes**: compared with the previous catalog. The first run establishes the baseline.
- **Deprecations**: provider pages in `sources.yaml` and API expiration dates.
- **News**: configured feeds from OpenRouter, Hugging Face, OpenAI, Google DeepMind, and NVIDIA. Feed coverage is not exhaustive; descriptions and dates are shown only when supplied by a source, and the source remains authoritative.
- **Best for**: rules over available model metadata, not benchmark rankings.
- **Provider locations**: not shown because the configured model catalogs do not provide a reliable, current source for provider headquarters or model availability by country.
- **Model profiles**: link to provider sites and documentation, plus model-specific OpenRouter or Hugging Face records when those identifiers are available. Links are omitted when a source does not provide them.

Data is retained: models that disappear are flagged as unlisted, price history is appended, and changes/news have bounded retention. A failed source keeps its last successful data and is recorded in source health.

Prefer APIs and RSS, store facts and links rather than copied text, respect source terms and robots.txt, and remove any source if its owner asks.
