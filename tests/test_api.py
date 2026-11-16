import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.app import _get_settings, _persist_dataset_snapshots, app


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage_env = patch.dict(
            os.environ,
            {"SUPABASE_URL": "", "SUPABASE_SECRET_KEY": "", "SUPABASE_SERVICE_ROLE_KEY": ""},
        )
        self.storage_env.start()
        transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.storage_env.stop()

    async def test_home_serves_frontend(self):
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("AI Models Tracker", response.text)
        self.assertIn('href="/admin" aria-label="Admin sign in">Admin</a>', response.text)
        self.assertIn('id="public-refresh"', response.text)
        self.assertIn('id="refresh-notice"', response.text)

    async def test_health_reports_catalog_readiness(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "catalog.json").write_text("{}", encoding="utf-8")
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                response = await self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["catalog_ready"])

    async def test_public_refresh_status_shows_next_run_when_scheduler_is_enabled(self):
        admin_env = {
            "ADMIN_USERNAME": "atlas-admin",
            "ADMIN_PASSWORD": "safe-test-password",
            "ADMIN_SESSION_SECRET": "test-secret-value-that-is-at-least-32-bytes",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, admin_env, clear=True):
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                response = await self.client.get("/api/refresh-status")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["scheduled"])
        self.assertIsNotNone(response.json()["next_run_at"])
        self.assertFalse(response.json()["running"])
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_public_refresh_status_has_no_next_run_when_scheduler_is_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                response = await self.client.get("/api/refresh-status")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["scheduled"])
        self.assertIsNone(response.json()["next_run_at"])
        self.assertTrue(response.json()["public_refresh_available"])
        self.assertIsNone(response.json()["public_refresh_after"])
        self.assertEqual(response.json()["cooldown_minutes"], 360)
        self.assertFalse(response.json()["running"])

    async def test_public_refresh_status_enforces_six_hour_cooldown(self):
        now = time.time()
        settings = {
            "interval_minutes": 360,
            "next_run_at": now + 3600,
            "last_started_at": now - 3 * 3600,
            "last_finished_at": now - 3 * 3600 + 60,
            "last_error": None,
            "last_icons_attempt_at": 0,
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            Path(temp_dir, "admin_settings.json").write_text(json.dumps(settings), encoding="utf-8")
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                response = await self.client.get("/api/refresh-status")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["public_refresh_available"])
        self.assertIsNotNone(response.json()["public_refresh_after"])

    async def test_public_refresh_status_uses_admin_configured_cooldown(self):
        now = time.time()
        settings = {
            "interval_minutes": 360,
            "public_refresh_cooldown_minutes": 120,
            "next_run_at": now + 3600,
            "last_started_at": now - 60 * 60,
            "last_finished_at": now - 60 * 60 + 60,
            "last_error": None,
            "last_icons_attempt_at": 0,
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            Path(temp_dir, "admin_settings.json").write_text(json.dumps(settings), encoding="utf-8")
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                response = await self.client.get("/api/refresh-status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cooldown_minutes"], 120)
        self.assertFalse(response.json()["public_refresh_available"])
        self.assertIsNotNone(response.json()["public_refresh_after"])

    async def test_public_refresh_can_be_started_without_admin_login(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                with patch("backend.app._start_refresh", return_value=True) as start_refresh:
                    response = await self.client.post("/api/refresh")

        self.assertEqual(response.status_code, 202)
        start_refresh.assert_called_once_with()

    async def test_public_refresh_rejects_requests_during_cooldown(self):
        now = time.time()
        settings = {
            "interval_minutes": 360,
            "next_run_at": now + 3600,
            "last_started_at": now - 3 * 3600,
            "last_finished_at": now - 3 * 3600 + 60,
            "last_error": None,
            "last_icons_attempt_at": 0,
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            Path(temp_dir, "admin_settings.json").write_text(json.dumps(settings), encoding="utf-8")
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                with patch("backend.app._start_refresh") as start_refresh:
                    response = await self.client.post("/api/refresh")

        self.assertEqual(response.status_code, 429)
        self.assertIn("retry-after", response.headers)
        start_refresh.assert_not_called()

    async def test_catalog_endpoint_returns_models(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "catalog.json").write_text(
                json.dumps({"models": [{"key": "sample"}]}), encoding="utf-8"
            )
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                response = await self.client.get("/api/data/catalog")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["models"], [{"key": "sample"}])
        self.assertEqual(response.headers["x-ai-data-source"], "local-only")

    async def test_catalog_endpoint_reads_remote_snapshot_when_configured(self):
        payload = {"models": [{"key": "remote-sample"}]}
        supabase_env = {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SECRET_KEY": "sb_secret_test-key",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, supabase_env):
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                with patch("backend.app.supabase_store.read_record", return_value=(True, payload)):
                    response = await self.client.get("/api/data/catalog")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        self.assertEqual(response.headers["x-ai-data-source"], "supabase")

    async def test_catalog_endpoint_seeds_remote_storage_from_local_cache(self):
        payload = {"models": [{"key": "local-sample"}]}
        supabase_env = {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SECRET_KEY": "sb_secret_test-key",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, supabase_env):
            Path(temp_dir, "catalog.json").write_text(json.dumps(payload), encoding="utf-8")
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                with patch("backend.app.supabase_store.read_record", return_value=(False, None)):
                    with patch("backend.app.supabase_store.write_records") as write_records:
                        response = await self.client.get("/api/data/catalog")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        self.assertEqual(response.headers["x-ai-data-source"], "supabase")
        write_records.assert_called_once_with({"catalog": payload})

    async def test_catalog_endpoint_uses_local_cache_when_remote_storage_fails(self):
        payload = {"models": [{"key": "cached-sample"}]}
        supabase_env = {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SECRET_KEY": "sb_secret_test-key",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, supabase_env):
            Path(temp_dir, "catalog.json").write_text(json.dumps(payload), encoding="utf-8")
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                with patch(
                    "backend.app.supabase_store.read_record",
                    side_effect=RuntimeError("remote unavailable"),
                ):
                    response = await self.client.get("/api/data/catalog")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        self.assertEqual(response.headers["x-ai-data-source"], "local-fallback")

    async def test_unknown_dataset_is_rejected(self):
        response = await self.client.get("/api/data/unknown")
        self.assertEqual(response.status_code, 404)

    async def test_source_health_is_not_exposed_in_the_public_api(self):
        response = await self.client.get("/api/data/status")
        self.assertEqual(response.status_code, 404)

    async def test_missing_changes_file_is_an_empty_change_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                response = await self.client.get("/api/data/changes")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    async def test_missing_required_dataset_returns_service_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                response = await self.client.get("/api/data/catalog")
        self.assertEqual(response.status_code, 503)

    async def test_admin_page_and_endpoints_require_login(self):
        admin_env = {
            "ADMIN_USERNAME": "atlas-admin",
            "ADMIN_PASSWORD": "safe-test-password",
            "ADMIN_SESSION_SECRET": "test-secret-value-that-is-at-least-32-bytes",
            "ADMIN_COOKIE_SECURE": "false",
        }
        with patch.dict(os.environ, admin_env):
            page = await self.client.get("/admin", follow_redirects=False)
            settings = await self.client.get("/api/admin/settings")

        self.assertEqual(page.status_code, 303)
        self.assertEqual(page.headers["location"], "/admin/login")
        self.assertEqual(settings.status_code, 401)

    async def test_admin_can_login_and_change_refresh_interval(self):
        admin_env = {
            "ADMIN_USERNAME": "atlas-admin",
            "ADMIN_PASSWORD": "safe-test-password",
            "ADMIN_SESSION_SECRET": "test-secret-value-that-is-at-least-32-bytes",
            "ADMIN_COOKIE_SECURE": "false",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, admin_env):
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                login = await self.client.post(
                    "/api/admin/login",
                    json={"username": "atlas-admin", "password": "safe-test-password"},
                )
                page = await self.client.get("/admin")
                settings = await self.client.get("/api/admin/settings")
                minute_presets = [
                    await self.client.put(
                        "/api/admin/settings",
                        json={"interval_minutes": minutes},
                    )
                    for minutes in (15, 30, 45)
                ]
                update = await self.client.put(
                    "/api/admin/settings",
                    json={"interval_minutes": 180, "public_refresh_cooldown_minutes": 120},
                )
                maximum = await self.client.put(
                    "/api/admin/settings",
                    json={"interval_minutes": 1440},
                )
                stored = json.loads(Path(temp_dir, "admin_settings.json").read_text(encoding="utf-8"))

        self.assertEqual(login.status_code, 200)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Refresh now", page.text)
        self.assertIn('id="public-cooldown"', page.text)
        self.assertEqual(settings.json()["interval_minutes"], 360)
        self.assertTrue(all(result.status_code == 200 for result in minute_presets))
        self.assertEqual(update.status_code, 200)
        self.assertEqual(update.json()["interval_minutes"], 180)
        self.assertEqual(update.json()["public_refresh_cooldown_minutes"], 120)
        self.assertEqual(maximum.status_code, 200)
        self.assertEqual(maximum.json()["interval_minutes"], 1440)
        self.assertEqual(maximum.json()["public_refresh_cooldown_minutes"], 120)
        self.assertEqual(stored["interval_minutes"], 1440)
        self.assertEqual(stored["public_refresh_cooldown_minutes"], 120)

    def test_legacy_hour_setting_migrates_to_minutes(self):
        legacy_settings = {
            "interval_hours": 6,
            "next_run_at": 1_800_000_000,
            "last_started_at": None,
            "last_finished_at": None,
            "last_error": None,
            "last_icons_attempt_at": 0,
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch("backend.app.DATA_DIR", Path(temp_dir)):
            Path(temp_dir, "admin_settings.json").write_text(
                json.dumps(legacy_settings),
                encoding="utf-8",
            )
            settings = _get_settings()
            stored = json.loads(Path(temp_dir, "admin_settings.json").read_text(encoding="utf-8"))

        self.assertEqual(settings["interval_minutes"], 360)
        self.assertEqual(settings["public_refresh_cooldown_minutes"], 360)
        self.assertNotIn("interval_hours", stored)
        self.assertEqual(stored["interval_minutes"], 360)
        self.assertEqual(stored["public_refresh_cooldown_minutes"], 360)

    def test_refresh_settings_restore_from_supabase_after_local_files_are_lost(self):
        stored_settings = {
            "interval_minutes": 180,
            "public_refresh_cooldown_minutes": 240,
            "next_run_at": 1_800_000_000,
            "last_started_at": 1_799_000_000,
            "last_finished_at": 1_799_000_100,
            "last_error": None,
            "last_icons_attempt_at": 0,
        }
        supabase_env = {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SECRET_KEY": "sb_secret_test-key",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, supabase_env):
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                with patch(
                    "backend.app.supabase_store.read_record",
                    return_value=(True, stored_settings),
                ):
                    settings = _get_settings()
                stored = json.loads(Path(temp_dir, "admin_settings.json").read_text(encoding="utf-8"))

        self.assertEqual(settings["interval_minutes"], 180)
        self.assertEqual(settings["public_refresh_cooldown_minutes"], 240)
        self.assertEqual(settings["last_started_at"], 1_799_000_000)
        self.assertEqual(stored, stored_settings)

    def test_refresh_persists_json_snapshots_to_supabase(self):
        supabase_env = {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SECRET_KEY": "sb_secret_test-key",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, supabase_env):
            for name in ("catalog", "best", "deprecations", "news", "changes", "meta", "status"):
                Path(temp_dir, f"{name}.json").write_text(json.dumps({"name": name}), encoding="utf-8")
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                with patch("backend.app.supabase_store.write_records") as write_records:
                    _persist_dataset_snapshots()

        records = write_records.call_args.args[0]
        self.assertEqual(set(records), {"catalog", "best", "deprecations", "news", "changes", "meta", "status"})
        self.assertEqual(records["catalog"], {"name": "catalog"})

    async def test_admin_refresh_rejects_invalid_interval_and_prevents_overlapping_runs(self):
        admin_env = {
            "ADMIN_USERNAME": "atlas-admin",
            "ADMIN_PASSWORD": "safe-test-password",
            "ADMIN_SESSION_SECRET": "test-secret-value-that-is-at-least-32-bytes",
            "ADMIN_COOKIE_SECURE": "false",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, admin_env):
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                await self.client.post(
                    "/api/admin/login",
                    json={"username": "atlas-admin", "password": "safe-test-password"},
                )
                invalid = await self.client.put(
                    "/api/admin/settings",
                    json={"interval_minutes": 50},
                )
                invalid_public_cooldown = await self.client.put(
                    "/api/admin/settings",
                    json={"interval_minutes": 360, "public_refresh_cooldown_minutes": 50},
                )
                with patch("backend.app._start_refresh", return_value=True):
                    started = await self.client.post("/api/admin/refresh")
                with patch("backend.app._start_refresh", return_value=False):
                    overlapping = await self.client.post("/api/admin/refresh")

        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid_public_cooldown.status_code, 422)
        self.assertEqual(started.status_code, 202)
        self.assertEqual(overlapping.status_code, 409)

    async def test_admin_lifespan_initializes_persistent_scheduler_settings(self):
        admin_env = {
            "ADMIN_USERNAME": "atlas-admin",
            "ADMIN_PASSWORD": "safe-test-password",
            "ADMIN_SESSION_SECRET": "test-secret-value-that-is-at-least-32-bytes",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, admin_env):
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                async with app.router.lifespan_context(app):
                    await asyncio.sleep(0)
                    settings = json.loads(
                        Path(temp_dir, "admin_settings.json").read_text(encoding="utf-8")
                    )

        self.assertEqual(settings["interval_minutes"], 360)
        self.assertGreater(settings["next_run_at"], 0)


if __name__ == "__main__":
    unittest.main()
