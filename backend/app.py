import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from backend import jobs as collector_jobs

PROJECT_ROOT = Path(os.environ.get("MODEL_ATLAS_ROOT", Path(__file__).resolve().parents[1])).resolve()
DATA_DIR = PROJECT_ROOT / "data"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
DATASETS = frozenset({"catalog", "best", "deprecations", "news", "changes", "meta"})
LOGGER = logging.getLogger(__name__)
ADMIN_COOKIE = "model_atlas_admin"
ADMIN_SESSION_TTL = 12 * 60 * 60
DEFAULT_REFRESH_INTERVAL_MINUTES = 6 * 60
REFRESH_INTERVALS_MINUTES = (15, 30, 45, *range(60, 24 * 60 + 1, 60))
SETTINGS_PATH = "admin_settings.json"

_settings_lock = threading.Lock()
_refresh_lock = threading.Lock()
_refresh_state_lock = threading.Lock()
_refresh_state = {"running": False}
_login_attempts_lock = threading.Lock()
_login_attempts = {}


class LoginRequest(BaseModel):
    username: str
    password: str


class RefreshSettingsRequest(BaseModel):
    interval_minutes: int = Field(strict=True, ge=15, le=24 * 60)

    @field_validator("interval_minutes")
    @classmethod
    def interval_must_be_available(cls, value: int) -> int:
        if value not in REFRESH_INTERVALS_MINUTES:
            raise ValueError("Choose 15, 30, or 45 minutes, or a whole-hour interval from 1 to 24 hours.")
        return value


def _admin_enabled() -> bool:
    return bool(
        os.environ.get("ADMIN_USERNAME")
        and os.environ.get("ADMIN_PASSWORD")
        and len(os.environ.get("ADMIN_SESSION_SECRET", "").encode("utf-8")) >= 32
    )


def _require_admin_configuration() -> None:
    if not _admin_enabled():
        raise HTTPException(status_code=503, detail="Admin access is not configured.")


def _session_signature(payload: str) -> str:
    secret = os.environ["ADMIN_SESSION_SECRET"].encode("utf-8")
    return hmac.new(secret, payload.encode("ascii"), hashlib.sha256).hexdigest()


def _create_session() -> str:
    expires = str(int(time.time()) + ADMIN_SESSION_TTL)
    payload = base64.urlsafe_b64encode(expires.encode("ascii")).rstrip(b"=").decode("ascii")
    return f"{payload}.{_session_signature(payload)}"


def _is_admin(request: Request) -> bool:
    if not _admin_enabled():
        return False
    token = request.cookies.get(ADMIN_COOKIE, "")
    try:
        payload, signature = token.split(".", 1)
        if not hmac.compare_digest(signature, _session_signature(payload)):
            return False
        expires = int(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return int(time.time()) < expires
    except (ValueError, UnicodeDecodeError):
        return False


def _require_admin(request: Request) -> None:
    _require_admin_configuration()
    if not _is_admin(request):
        raise HTTPException(status_code=401, detail="Admin authentication required.")


def _settings_path() -> Path:
    return DATA_DIR / SETTINGS_PATH


def _write_settings_unlocked(settings: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = DATA_DIR / ".admin_settings.tmp"
    temp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    temp.replace(_settings_path())


def _read_settings_unlocked() -> dict:
    path = _settings_path()
    if not path.exists():
        settings = {
            "interval_minutes": DEFAULT_REFRESH_INTERVAL_MINUTES,
            "next_run_at": time.time() + DEFAULT_REFRESH_INTERVAL_MINUTES * 60,
            "last_started_at": None,
            "last_finished_at": None,
            "last_error": None,
            "last_icons_attempt_at": 0,
        }
        _write_settings_unlocked(settings)
        return settings

    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        migrated_interval = False
        if "interval_minutes" not in settings:
            previous_hours = settings.pop("interval_hours", None)
            if type(previous_hours) is int:
                settings["interval_minutes"] = previous_hours * 60
                migrated_interval = True
        elif "interval_hours" in settings:
            settings.pop("interval_hours")
            migrated_interval = True
        next_run_at = settings.get("next_run_at")
        last_icons_attempt_at = settings.get("last_icons_attempt_at", 0)
        if (
            type(settings.get("interval_minutes")) is not int
            or settings["interval_minutes"] not in REFRESH_INTERVALS_MINUTES
            or type(next_run_at) not in (int, float)
            or type(last_icons_attempt_at) not in (int, float)
            or not 0 <= next_run_at < 1_000_000_000_000
            or not 0 <= last_icons_attempt_at < 1_000_000_000_000
        ):
            raise ValueError("Invalid refresh settings.")
        settings.setdefault("last_icons_attempt_at", 0)
        if migrated_interval:
            _write_settings_unlocked(settings)
    except (OSError, json.JSONDecodeError, AttributeError, ValueError) as error:
        LOGGER.exception("Could not read refresh settings")
        raise RuntimeError("Refresh settings are unavailable or invalid.") from error
    return settings


def _get_settings() -> dict:
    with _settings_lock:
        return _read_settings_unlocked()


def _update_settings(**updates) -> dict:
    with _settings_lock:
        settings = _read_settings_unlocked()
        settings.update(updates)
        _write_settings_unlocked(settings)
        return settings


def _settings_response() -> dict:
    settings = _get_settings()
    with _refresh_state_lock:
        running = _refresh_state["running"]
    return {
        "interval_minutes": settings["interval_minutes"],
        "next_run_at": datetime.fromtimestamp(settings["next_run_at"], timezone.utc).isoformat(),
        "last_started_at": settings.get("last_started_at"),
        "last_finished_at": settings.get("last_finished_at"),
        "last_error": settings.get("last_error"),
        "running": running,
    }


def _perform_refresh() -> None:
    started_at = time.time()
    error_message = None
    try:
        for job in ("prices", "deprecations", "news"):
            collector_jobs.main([job])

        settings = _get_settings()
        if started_at - settings.get("last_icons_attempt_at", 0) >= 24 * 3600:
            collector_jobs.main(["icons"])
            _update_settings(last_icons_attempt_at=started_at)
    except Exception as error:
        error_message = str(error)[:300]
        LOGGER.exception("Scheduled data refresh failed")
    finally:
        finished_at = time.time()
        with _refresh_state_lock:
            _refresh_state["running"] = False
        try:
            _update_settings(last_finished_at=finished_at, last_error=error_message)
        except Exception:
            LOGGER.exception("Could not save refresh completion status")
        _refresh_lock.release()


def _start_refresh() -> bool:
    if not _refresh_lock.acquire(blocking=False):
        return False
    now = time.time()
    try:
        settings = _get_settings()
        _update_settings(
            next_run_at=now + settings["interval_minutes"] * 60,
            last_started_at=now,
            last_error=None,
        )
        with _refresh_state_lock:
            _refresh_state["running"] = True
        threading.Thread(target=_perform_refresh, name="ai-models-tracker-refresh", daemon=True).start()
    except Exception:
        with _refresh_state_lock:
            _refresh_state["running"] = False
        _refresh_lock.release()
        raise
    return True


async def _refresh_scheduler() -> None:
    while True:
        settings = _get_settings()
        delay = settings["next_run_at"] - time.time()
        if delay <= 0:
            if not _start_refresh():
                await asyncio.sleep(5)
        else:
            await asyncio.sleep(min(delay, 60))


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler = None
    if _admin_enabled():
        _get_settings()
        scheduler = asyncio.create_task(_refresh_scheduler())
    try:
        yield
    finally:
        if scheduler:
            scheduler.cancel()
            try:
                await scheduler
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="AI Models Tracker API",
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)

app.mount(
    "/data/icons",
    StaticFiles(directory=DATA_DIR / "icons", check_dir=False),
    name="provider-icons",
)


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    index = FRONTEND_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=503, detail="The web interface is unavailable.")
    return FileResponse(index, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


@app.get("/admin/login", include_in_schema=False)
def admin_login_page() -> FileResponse:
    page = FRONTEND_DIR / "admin-login.html"
    if not page.is_file():
        raise HTTPException(status_code=503, detail="The admin login page is unavailable.")
    return FileResponse(page, headers={"Cache-Control": "no-store"})


@app.get("/admin", include_in_schema=False)
def admin_page(request: Request) -> Response:
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303, headers={"Cache-Control": "no-store"})
    page = FRONTEND_DIR / "admin.html"
    if not page.is_file():
        raise HTTPException(status_code=503, detail="The admin page is unavailable.")
    return FileResponse(page, headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health() -> JSONResponse:
    catalog_ready = (DATA_DIR / "catalog.json").is_file()
    status_code = 200 if catalog_ready else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if catalog_ready else "degraded",
            "catalog_ready": catalog_ready,
        },
    )


@app.get("/api/refresh-status")
def public_refresh_status() -> JSONResponse:
    if not _admin_enabled():
        return JSONResponse(
            content={"scheduled": False, "next_run_at": None, "running": False},
            headers={"Cache-Control": "no-store"},
        )
    try:
        settings = _get_settings()
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    with _refresh_state_lock:
        running = _refresh_state["running"]
    return JSONResponse(
        content={
            "scheduled": True,
            "next_run_at": datetime.fromtimestamp(settings["next_run_at"], timezone.utc).isoformat(),
            "running": running,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/admin/login")
def admin_login(credentials: LoginRequest, request: Request, response: Response) -> dict:
    _require_admin_configuration()
    address = request.client.host if request.client else "unknown"
    now = time.time()
    with _login_attempts_lock:
        attempts, window_start = _login_attempts.get(address, (0, now))
        if now - window_start >= 15 * 60:
            attempts, window_start = 0, now
        if attempts >= 10:
            raise HTTPException(status_code=429, detail="Too many login attempts. Try again in 15 minutes.")
        username_ok = hmac.compare_digest(
            credentials.username.encode("utf-8"), os.environ["ADMIN_USERNAME"].encode("utf-8")
        )
        password_ok = hmac.compare_digest(
            credentials.password.encode("utf-8"), os.environ["ADMIN_PASSWORD"].encode("utf-8")
        )
        if not (username_ok and password_ok):
            _login_attempts[address] = (attempts + 1, window_start)
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        _login_attempts.pop(address, None)
    response.set_cookie(
        ADMIN_COOKIE,
        _create_session(),
        max_age=ADMIN_SESSION_TTL,
        httponly=True,
        secure=os.environ.get("ADMIN_COOKIE_SECURE", "true").lower() != "false",
        samesite="strict",
        path="/",
    )
    return {"ok": True}


@app.post("/api/admin/logout")
def admin_logout(request: Request, response: Response) -> dict:
    _require_admin(request)
    response.delete_cookie(
        ADMIN_COOKIE,
        httponly=True,
        secure=os.environ.get("ADMIN_COOKIE_SECURE", "true").lower() != "false",
        samesite="strict",
        path="/",
    )
    return {"ok": True}


@app.get("/api/admin/settings")
def admin_settings(request: Request) -> JSONResponse:
    _require_admin(request)
    try:
        settings = _settings_response()
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return JSONResponse(content=settings, headers={"Cache-Control": "no-store"})


@app.put("/api/admin/settings")
def update_admin_settings(update: RefreshSettingsRequest, request: Request) -> JSONResponse:
    _require_admin(request)
    try:
        _update_settings(
            interval_minutes=update.interval_minutes,
            next_run_at=time.time() + update.interval_minutes * 60,
        )
        settings = _settings_response()
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return JSONResponse(content=settings, headers={"Cache-Control": "no-store"})


@app.post("/api/admin/refresh")
def refresh_now(request: Request) -> JSONResponse:
    _require_admin(request)
    try:
        started = _start_refresh()
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if not started:
        raise HTTPException(status_code=409, detail="A refresh is already running.")
    return JSONResponse(content=_settings_response(), status_code=202, headers={"Cache-Control": "no-store"})


@app.get("/api/admin/status")
def admin_status(request: Request) -> JSONResponse:
    _require_admin(request)
    try:
        with (DATA_DIR / "status.json").open(encoding="utf-8") as source:
            sources = json.load(source)
    except FileNotFoundError:
        sources = {}
    except (OSError, json.JSONDecodeError) as error:
        LOGGER.exception("Could not read source health")
        raise HTTPException(status_code=503, detail="Source health is temporarily unavailable.") from error
    try:
        settings = _settings_response()
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return JSONResponse(
        content={"refresh": settings, "sources": sources},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/data/{dataset}")
def read_dataset(dataset: str) -> JSONResponse:
    if dataset not in DATASETS:
        raise HTTPException(status_code=404, detail="Dataset not found.")

    path = DATA_DIR / f"{dataset}.json"
    if not path.is_file() and dataset == "changes":
        return JSONResponse(content=[], headers={"Cache-Control": "no-cache"})
    if not path.is_file():
        raise HTTPException(status_code=503, detail=f"Dataset '{dataset}' is not available yet.")

    try:
        with path.open(encoding="utf-8") as source:
            payload = json.load(source)
    except (OSError, json.JSONDecodeError):
        LOGGER.exception("Could not read dataset %s", dataset)
        raise HTTPException(status_code=503, detail=f"Dataset '{dataset}' is temporarily unavailable.")

    return JSONResponse(content=payload, headers={"Cache-Control": "no-cache"})
