import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.app import _get_settings, app


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        transport = httpx.ASGITransport(app=app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_home_serves_frontend(self):
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("AI Models Tracker", response.text)
        self.assertIn('href="/admin" aria-label="Admin sign in">Admin</a>', response.text)

    async def test_health_reports_catalog_readiness(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "catalog.json").write_text("{}", encoding="utf-8")
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                response = await self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["catalog_ready"])

    async def test_catalog_endpoint_returns_models(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "catalog.json").write_text(
                json.dumps({"models": [{"key": "sample"}]}), encoding="utf-8"
            )
            with patch("backend.app.DATA_DIR", Path(temp_dir)):
                response = await self.client.get("/api/data/catalog")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["models"], [{"key": "sample"}])

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
                    json={"interval_minutes": 180},
                )
                maximum = await self.client.put(
                    "/api/admin/settings",
                    json={"interval_minutes": 1440},
                )
                stored = json.loads(Path(temp_dir, "admin_settings.json").read_text(encoding="utf-8"))

        self.assertEqual(login.status_code, 200)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Refresh now", page.text)
        self.assertEqual(settings.json()["interval_minutes"], 360)
        self.assertTrue(all(result.status_code == 200 for result in minute_presets))
        self.assertEqual(update.status_code, 200)
        self.assertEqual(update.json()["interval_minutes"], 180)
        self.assertEqual(maximum.status_code, 200)
        self.assertEqual(maximum.json()["interval_minutes"], 1440)
        self.assertEqual(stored["interval_minutes"], 1440)

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
        self.assertNotIn("interval_hours", stored)
        self.assertEqual(stored["interval_minutes"], 360)

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
                with patch("backend.app._start_refresh", return_value=True):
                    started = await self.client.post("/api/admin/refresh")
                with patch("backend.app._start_refresh", return_value=False):
                    overlapping = await self.client.post("/api/admin/refresh")

        self.assertEqual(invalid.status_code, 422)
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
