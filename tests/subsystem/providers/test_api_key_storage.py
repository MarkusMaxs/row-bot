"""Focused tests for keyring-backed API key storage."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

import pytest


pytestmark = pytest.mark.platform
_KEYS_SET_HERE = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ATLASCLOUD_API_KEY")


class FakeKeyring:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        if self.fail:
            raise RuntimeError("keyring unavailable")
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        if self.fail:
            raise RuntimeError("keyring unavailable")
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        if self.fail:
            raise RuntimeError("keyring unavailable")
        self.values.pop((service, account), None)


class ApiKeyStorageTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def _key_modules_for_a_fresh_folder(self, tmp_path, monkeypatch, reload_for_data_dir):
        # set_key writes the environment itself: start without these keys, and put back any the shell had.
        for key in _KEYS_SET_HERE:
            monkeypatch.delenv(key, raising=False)
        self.secret_store, self.api_keys, self.plugin_state = reload_for_data_dir(
            tmp_path, "row_bot.secret_store", "row_bot.api_keys", "row_bot.plugins.state",
        )
        self.backend = FakeKeyring()
        self.secret_store._set_backend_for_tests(self.backend)
        yield
        self.secret_store._set_backend_for_tests(None)
        for key in _KEYS_SET_HERE:
            os.environ.pop(key, None)

    def test_set_key_writes_keyring_and_metadata_only(self) -> None:
        self.api_keys.set_key("OPENAI_API_KEY", "sk-test-secret-1234")

        self.assertEqual(self.api_keys.get_key("OPENAI_API_KEY"), "sk-test-secret-1234")
        metadata = json.loads(Path(self.api_keys.KEYS_PATH).read_text(encoding="utf-8"))
        self.assertEqual(metadata["version"], 2)
        self.assertEqual(metadata["keys"]["OPENAI_API_KEY"]["fingerprint"], "****1234")
        self.assertNotIn("sk-test-secret-1234", json.dumps(metadata))

    def test_legacy_plaintext_file_is_migrated_to_keyring(self) -> None:
        Path(self.api_keys.KEYS_PATH).write_text(
            json.dumps({"OPENAI_API_KEY": "sk-legacy-secret"}),
            encoding="utf-8",
        )

        keys = self.api_keys._load_keys()
        metadata = json.loads(Path(self.api_keys.KEYS_PATH).read_text(encoding="utf-8"))

        self.assertEqual(keys["OPENAI_API_KEY"], "sk-legacy-secret")
        self.assertEqual(metadata["version"], 2)
        self.assertNotIn("sk-legacy-secret", json.dumps(metadata))

    def test_legacy_plaintext_remains_available_when_keyring_fails(self) -> None:
        self.secret_store._set_backend_for_tests(FakeKeyring(fail=True))
        Path(self.api_keys.KEYS_PATH).write_text(
            json.dumps({"ANTHROPIC_API_KEY": "sk-ant-legacy"}),
            encoding="utf-8",
        )

        self.assertEqual(self.api_keys.get_key("ANTHROPIC_API_KEY"), "sk-ant-legacy")
        self.assertIn("legacy plaintext", self.api_keys.get_storage_warning())
        raw = json.loads(Path(self.api_keys.KEYS_PATH).read_text(encoding="utf-8"))
        self.assertEqual(raw["ANTHROPIC_API_KEY"], "sk-ant-legacy")

    def test_new_key_is_session_only_when_keyring_fails(self) -> None:
        self.secret_store._set_backend_for_tests(FakeKeyring(fail=True))

        self.api_keys.set_key("OPENAI_API_KEY", "sk-session-only")

        self.assertEqual(self.api_keys.get_key("OPENAI_API_KEY"), "sk-session-only")
        self.assertFalse(Path(self.api_keys.KEYS_PATH).exists())
        self.assertIn("session only", self.api_keys.get_storage_warning())

    def test_saved_key_names_lists_saved_session_and_known_names_never_values(self) -> None:
        self.api_keys.set_key("FIXTURE_SAVED_NAME", "fixture-saved-secret")
        self.secret_store._set_backend_for_tests(FakeKeyring(fail=True))
        self.api_keys.set_key("FIXTURE_SESSION_NAME", "fixture-session-secret")
        try:
            names = self.api_keys.saved_key_names()
            self.assertTrue({"FIXTURE_SAVED_NAME", "FIXTURE_SESSION_NAME", "OPENAI_API_KEY",
                             "TELEGRAM_BOT_TOKEN"} <= names)
            self.assertFalse({"fixture-saved-secret", "fixture-session-secret"} & names)
        finally:
            for name in ("FIXTURE_SAVED_NAME", "FIXTURE_SESSION_NAME"):
                os.environ.pop(name, None)

    def test_missing_keyring_backend_does_not_log_traceback_on_read(self) -> None:
        self.secret_store._set_backend_for_tests(FakeKeyring(fail=True))

        with self.assertNoLogs("secret_store", level="WARNING"):
            self.assertEqual(self.api_keys.get_key("OPENAI_API_KEY"), "")

        self.assertIn("Secure API key storage is unavailable", self.api_keys.get_storage_warning())

    def test_delete_key_removes_keyring_metadata_and_environment(self) -> None:
        self.api_keys.set_key("OPENAI_API_KEY", "sk-delete-me")
        self.api_keys.delete_key("OPENAI_API_KEY")

        self.assertEqual(self.api_keys.get_key("OPENAI_API_KEY"), "")
        self.assertNotIn("OPENAI_API_KEY", os.environ)
        metadata = json.loads(Path(self.api_keys.KEYS_PATH).read_text(encoding="utf-8"))
        self.assertNotIn("OPENAI_API_KEY", metadata.get("keys", {}))

    def test_get_key_uses_metadata_service_when_it_differs_from_default(self) -> None:
        service = "row-bot:test-profile-service"
        self.backend.values[(service, "api_keys:ATLASCLOUD_API_KEY")] = "atlas-secret-1234"
        Path(self.api_keys.KEYS_PATH).write_text(
            json.dumps({
                "version": 2,
                "storage": "keyring",
                "service": service,
                "keys": {
                    "ATLASCLOUD_API_KEY": {
                        "configured": True,
                        "fingerprint": "****1234",
                    }
                },
            }),
            encoding="utf-8",
        )

        self.assertEqual(self.api_keys.get_key("ATLASCLOUD_API_KEY"), "atlas-secret-1234")

    def test_plugin_secret_writes_keyring_and_metadata_only(self) -> None:
        self.plugin_state.set_plugin_secret("plugin-a", "API_KEY", "plug-secret-1234")

        self.assertEqual(self.plugin_state.get_plugin_secret("plugin-a", "API_KEY"), "plug-secret-1234")
        metadata = json.loads(Path(self.plugin_state._SECRETS_PATH).read_text(encoding="utf-8"))
        self.assertEqual(metadata["version"], 2)
        self.assertEqual(metadata["plugins"]["plugin-a"]["API_KEY"]["fingerprint"], "****1234")
        self.assertNotIn("plug-secret-1234", json.dumps(metadata))

    def test_plugin_legacy_plaintext_file_is_migrated_to_keyring(self) -> None:
        Path(self.plugin_state._SECRETS_PATH).write_text(
            json.dumps({"plugin-a": {"API_KEY": "plug-legacy-secret"}}),
            encoding="utf-8",
        )
        self.plugin_state._reset()

        self.assertEqual(self.plugin_state.get_plugin_secret("plugin-a", "API_KEY"), "plug-legacy-secret")
        metadata = json.loads(Path(self.plugin_state._SECRETS_PATH).read_text(encoding="utf-8"))

        self.assertEqual(metadata["version"], 2)
        self.assertNotIn("plug-legacy-secret", json.dumps(metadata))

    def test_plugin_legacy_plaintext_remains_available_when_keyring_fails(self) -> None:
        self.secret_store._set_backend_for_tests(FakeKeyring(fail=True))
        Path(self.plugin_state._SECRETS_PATH).write_text(
            json.dumps({"plugin-a": {"API_KEY": "plug-legacy-secret"}}),
            encoding="utf-8",
        )
        self.plugin_state._reset()

        self.assertEqual(self.plugin_state.get_plugin_secret("plugin-a", "API_KEY"), "plug-legacy-secret")
        raw = json.loads(Path(self.plugin_state._SECRETS_PATH).read_text(encoding="utf-8"))
        self.assertEqual(raw["plugin-a"]["API_KEY"], "plug-legacy-secret")


if __name__ == "__main__":
    unittest.main()
