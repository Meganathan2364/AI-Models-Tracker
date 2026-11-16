"""Persistence for JSON snapshots in a Supabase Postgres table."""
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

TABLE = "ai_model_snapshots"


def _configuration() -> tuple[str, str] | None:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = (
        os.environ.get("SUPABASE_SECRET_KEY", "").strip()
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    )
    if not url and not key:
        return None
    if not url or not key:
        raise RuntimeError("Set both SUPABASE_URL and SUPABASE_SECRET_KEY.")
    if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc:
        raise RuntimeError("SUPABASE_URL must be an absolute HTTP or HTTPS URL.")
    return url, key


def is_configured() -> bool:
    return _configuration() is not None


def _headers(key: str) -> dict[str, str]:
    headers = {
        "apikey": key,
        "Content-Type": "application/json",
    }
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def read_record(name: str) -> tuple[bool, object]:
    configuration = _configuration()
    if configuration is None:
        raise RuntimeError("Supabase storage is not configured.")
    url, key = configuration
    try:
        response = requests.get(
            f"{url}/rest/v1/{TABLE}",
            params={"select": "payload", "name": f"eq.{name}"},
            headers=_headers(key),
            timeout=(5, 20),
        )
        response.raise_for_status()
        rows = response.json()
    except (requests.RequestException, ValueError) as error:
        raise RuntimeError(f"Could not read '{name}' from Supabase storage.") from error
    if not isinstance(rows, list):
        raise RuntimeError("Supabase returned an invalid dataset response.")
    if not rows:
        return False, None
    if not isinstance(rows[0], dict) or "payload" not in rows[0]:
        raise RuntimeError("Supabase returned an invalid dataset record.")
    return True, rows[0]["payload"]


def write_records(records: dict[str, object]) -> None:
    if not records:
        return
    configuration = _configuration()
    if configuration is None:
        raise RuntimeError("Supabase storage is not configured.")
    url, key = configuration
    updated_at = datetime.now(timezone.utc).isoformat()
    rows = [
        {"name": name, "payload": payload, "updated_at": updated_at}
        for name, payload in records.items()
    ]
    headers = {
        **_headers(key),
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    try:
        response = requests.post(
            f"{url}/rest/v1/{TABLE}",
            params={"on_conflict": "name"},
            headers=headers,
            json=rows,
            timeout=(5, 30),
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise RuntimeError("Could not save refreshed datasets to Supabase storage.") from error
