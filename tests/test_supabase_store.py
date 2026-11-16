import os
import unittest
from unittest.mock import Mock, patch

from backend import supabase_store


class SupabaseStoreTests(unittest.TestCase):
    def test_storage_is_optional_when_credentials_are_not_set(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(supabase_store.is_configured())

    def test_partial_configuration_is_rejected(self):
        with patch.dict(os.environ, {"SUPABASE_URL": "https://example.supabase.co"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Set both SUPABASE_URL"):
                supabase_store.is_configured()

    def test_read_record_returns_the_saved_payload(self):
        response = Mock()
        response.json.return_value = [{"payload": {"models": [{"key": "sample"}]}}]
        with patch.dict(
            os.environ,
            {
                "SUPABASE_URL": "https://example.supabase.co/",
                "SUPABASE_SECRET_KEY": "sb_secret_test-key",
            },
            clear=True,
        ):
            with patch("backend.supabase_store.requests.get", return_value=response) as get:
                found, payload = supabase_store.read_record("catalog")

        self.assertTrue(found)
        self.assertEqual(payload, {"models": [{"key": "sample"}]})
        self.assertEqual(get.call_args.args[0], "https://example.supabase.co/rest/v1/ai_model_snapshots")
        self.assertEqual(get.call_args.kwargs["headers"]["apikey"], "sb_secret_test-key")
        self.assertNotIn("Authorization", get.call_args.kwargs["headers"])

    def test_write_records_upserts_snapshots(self):
        response = Mock()
        with patch.dict(
            os.environ,
            {
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SECRET_KEY": "sb_secret_test-key",
            },
            clear=True,
        ):
            with patch("backend.supabase_store.requests.post", return_value=response) as post:
                supabase_store.write_records({"catalog": {"models": []}})

        self.assertEqual(post.call_args.kwargs["params"], {"on_conflict": "name"})
        self.assertEqual(post.call_args.kwargs["json"][0]["name"], "catalog")
        self.assertEqual(
            post.call_args.kwargs["headers"]["Prefer"],
            "resolution=merge-duplicates,return=minimal",
        )

    def test_legacy_service_role_key_is_supported(self):
        headers = supabase_store._headers("eyJlegacy-token")
        self.assertEqual(headers["Authorization"], "Bearer eyJlegacy-token")


if __name__ == "__main__":
    unittest.main()
