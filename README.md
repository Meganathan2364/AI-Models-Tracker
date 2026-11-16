# AI Models Tracker

AI Models Tracker brings AI model catalogs, pricing, capabilities, provider news, and retirement information together in one application. It gathers information from configured public APIs, catalogs, feeds, and provider pages, then presents it in a searchable, comparable catalog. BeautifulSoup and Playwright are used where webpage parsing or browser-based collection is needed.

The configured catalogs represent models from roughly 270 providers and resellers, including records from OpenRouter and models.dev, plus direct Alibaba Cloud Model Studio coverage for Qwen. Coverage changes with the upstream sources and is not a guarantee that every available model or provider is included.

## Application features

- **Overview:** Counts tracked models and providers, highlights upcoming retirements, and shows recent model additions, price changes, listing changes, and deprecation notices.
- **Model catalog:** Search by model, provider, or capability. Filter by provider, output type, release/added/changed dates, reasoning, open weights, and whether a model is still listed. Use quick or custom date ranges, sort by name, provider, context size, pricing, or dates, and load results in batches of 100.
- **Model profiles:** View descriptions, provider and source links, identifiers and aliases, context and output limits, release and update dates, reported capabilities and modalities, supported parameters, and available pricing.
- **Pricing tools:** Review input/output and other reported token prices, examine available price history, and estimate input/output cost using token counts.
- **Best for:** Browse rule-based matches for configured purposes and the top matching model per provider. These matches use available metadata; they are not benchmark rankings.
- **Compare:** Select two to four models to compare context and output limits, input/output costs, and reported capabilities. Missing source information is labeled “Not reported,” not treated as proof that a feature is unavailable.
- **Deprecations:** Review upcoming and past retirement dates, source details, and models no longer listed by tracked sources. Removed entries can be retained for history.
- **News:** Browse recent stories from configured feeds and filter by source, with links to the original articles.
- **Change and source tracking:** Detect changes against previous collections, retain price history, and show source health to administrators. When a source fails, its last successful data is retained.
- **Refresh controls:** Visitors can manually refresh when the public cooldown expires. The catalog shows the refresh countdown and a notice when a refresh is available. Administrators can start a refresh immediately and separately configure the automatic refresh interval and public refresh cooldown.

## Refresh behavior and persistence

The automatic refresh schedule and public manual-refresh cooldown are separate settings. Both default to six hours. Administrators can change either setting in the Admin dashboard; supported choices are 15, 30, or 45 minutes, or whole-hour intervals from 1 to 24 hours.

The scheduler runs inside the web service. Render Free services sleep when idle and do not run scheduled jobs while asleep. A visit can wake the service, after which an administrator or visitor can request a refresh when permitted. A successful refresh writes the collected datasets and refresh settings to Supabase so they remain available across Render sleeps, restarts, and deploys.

When Supabase has no saved snapshots yet, the app can seed it from the bundled local JSON data. The app reads saved Supabase data first and falls back to local JSON if Supabase is unavailable. In fallback mode, refreshed data may not persist until Supabase is available again. Supabase Free projects can pause after extended inactivity, so Supabase is durable storage but not an always-running refresh service.

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

Model records, prices, capabilities, and retirement dates are collected from configured upstream APIs and provider pages; they are not maintained as model entries in the application code. The JSON files under `data/` are the local collection cache. Run `python -m backend.jobs all` before starting locally for a fresh collection. The catalog displays when its data was last updated and the source identifiers represented in the records.

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

`start.sh` fetches data if `data/catalog.json` is missing, then starts Uvicorn on Render's `PORT` (default `10000`). For a Render service without a persistent disk, configure Supabase as described below to keep refreshed datasets and refresh settings across sleeps, restarts, and deploys. A persistent Render disk is an alternative, but requires a paid service. Run exactly one application instance/worker so the in-process scheduler does not run more than once.

Configure these variables in the host's environment settings:

- `ADMIN_USERNAME`: the admin account name.
- `ADMIN_PASSWORD`: a strong, unique admin password.
- `ADMIN_SESSION_SECRET`: a random secret of at least 32 bytes (for example, generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`).
- `ADMIN_COOKIE_SECURE`: leave unset or set to `true` in production. Set it to `false` only for local HTTP development.

Open `/admin/login` to sign in; the admin dashboard is at `/admin`. The admin page and every admin API require a valid, HTTP-only signed session; unauthenticated visitors are redirected to the login form and cannot load the admin page. The scheduler starts only when all three admin environment variables are configured. Provider icons are refreshed no more than once per day.

### Supabase persistent storage

Create a Supabase project and run this SQL once in its SQL Editor:

```sql
create table if not exists public.ai_model_snapshots (
  name text primary key,
  payload jsonb not null,
  updated_at timestamptz not null default now()
);
alter table public.ai_model_snapshots enable row level security;
grant all on table public.ai_model_snapshots to service_role;
```

Add these variables to the Render service's environment settings:

- `SUPABASE_URL`: the Supabase project URL.
- `SUPABASE_SECRET_KEY`: a Supabase secret API key (`sb_secret_...`). Keep this server-side; never put it in frontend code or commit it to the repository.

The app also accepts the legacy `SUPABASE_SERVICE_ROLE_KEY` variable for existing deployments. Prefer `SUPABASE_SECRET_KEY` for new setup. The secret key is used only by the backend to read and write snapshots and refresh settings.

The GitHub Actions workflow in `.github/workflows/fetch.yml` is manual-only; it no longer competes with the admin-configured scheduler. GitHub Pages cannot host the Python API.

The public API exposes only the named datasets under `/api/data/{dataset}`. Admin credentials are configured only through deployment environment secrets and are never stored in the repository. If a data file is unavailable, the API reports an explicit service error instead of returning an invalid response.

## Data sources and limits

- **Models and prices**: OpenRouter Models API and providers/resellers in models.dev, merged by normalized model name; Qwen also has direct Alibaba Cloud Model Studio coverage.
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
