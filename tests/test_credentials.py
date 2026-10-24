"""Offline checks for CLI credential compatibility and account isolation."""

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.accounts import AccountManager
from app.credentials import account_config_dir, read_cli_auth_token, write_cli_credentials


class TestCredentialStorage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config"

    def test_migrates_old_format_to_valid_default_profile(self):
        self.config.mkdir()
        path = self.config / "credentials.json"
        path.write_text(json.dumps({"accessToken": "old-token", "tokenStore": "file"}))
        write_cli_credentials(str(self.config), 'new-"token\\value')
        data = json.loads(path.read_text())
        self.assertEqual(data, {"default": {"authToken": 'new-"token\\value', "email": "", "name": ""}})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o700)

    def test_replaces_keychain_pointer_and_preserves_metadata(self):
        self.config.mkdir()
        path = self.config / "credentials.json"
        path.write_text(json.dumps({
            "default": {"tokenStore": "keychain", "id": "user-1", "email": "user@example.com", "name": None, "fingerprintId": "existing-fingerprint"},
            "other": {"retained": True},
        }))
        write_cli_credentials(str(self.config), "new-token")
        profile = json.loads(path.read_text())["default"]
        self.assertEqual(profile["authToken"], "new-token")
        self.assertNotIn("tokenStore", profile)
        self.assertEqual(profile["id"], "user-1")
        self.assertEqual(profile["email"], "user@example.com")
        self.assertEqual(profile["fingerprintId"], "existing-fingerprint")
        self.assertEqual(json.loads(path.read_text())["other"], {"retained": True})

    def test_missing_token_preserves_existing_login(self):
        self.config.mkdir()
        path = self.config / "credentials.json"
        original = '{"default":{"email":"u@example.com","tokenStore":"keychain"}}'
        path.write_text(original)
        for token in (None, "", " ", "unauthenticated"):
            self.assertIsNone(write_cli_credentials(str(self.config), token))
            self.assertEqual(path.read_text(), original)

    def test_failed_atomic_replace_preserves_original_and_cleans_temp(self):
        write_cli_credentials(str(self.config), "old-token")
        original = (self.config / "credentials.json").read_bytes()
        with patch("app.credentials.os.replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                write_cli_credentials(str(self.config), "new-token")
        self.assertEqual((self.config / "credentials.json").read_bytes(), original)
        self.assertEqual(list(self.config.glob(".credentials-*.tmp")), [])

    def test_rejects_credentials_symlink_without_touching_target(self):
        self.config.mkdir()
        target = self.root / "other-account.json"
        target.write_text('{"default":{"authToken":"other-account"}}')
        (self.config / "credentials.json").symlink_to(target)
        with self.assertRaises(ValueError):
            write_cli_credentials(str(self.config), "new-token")
        self.assertEqual(json.loads(target.read_text())["default"]["authToken"], "other-account")


class TestAccountCredentialIsolation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sandboxes = self.root / "accounts"
        self.config_file = self.root / "accounts.json"
        sandbox_patch = patch("app.accounts.BASE_SANDBOX_DIR", str(self.sandboxes))
        sandbox_patch.start()
        self.addCleanup(sandbox_patch.stop)
        proxy_patch = patch("app.accounts.global_proxy_manager")
        self.proxy = proxy_patch.start()
        self.proxy.nodes = []
        self.proxy.singbox_bin = None
        self.addCleanup(proxy_patch.stop)
        env_patch = patch.dict(os.environ, {"HOME": str(self.root / "native-home")})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in ("CODEBUFF_API_KEY", "FREEBUFF_AUTH_TOKEN", "FREEBUFF_CONFIG_DIR"):
            os.environ.pop(key, None)

    def write_accounts(self, accounts):
        self.config_file.write_text(json.dumps({"accounts": accounts}))

    def test_each_account_uses_own_private_config_despite_host_override(self):
        shared = self.root / "shared-config"
        os.environ["FREEBUFF_CONFIG_DIR"] = str(shared)
        self.write_accounts([
            {"id": "one", "name": "One", "token": "token-one"},
            {"id": "two", "name": "Two", "token": "token-two"},
        ])
        manager = AccountManager(str(self.config_file))
        accounts = manager.load_accounts()
        self.assertEqual(len(accounts), 2)
        for account in accounts:
            config = account_config_dir(account.home_dir)
            self.assertEqual(read_cli_auth_token(config), account.config.token)
            self.assertEqual(stat.S_IMODE(Path(account.home_dir).stat().st_mode), 0o700)
        self.assertFalse(shared.exists())
        self.proxy.fetch_subscription.assert_not_called()

    def test_fallback_reads_native_default_profile(self):
        native = self.root / "native-config"
        os.environ["FREEBUFF_CONFIG_DIR"] = str(native)
        write_cli_credentials(str(native), "native-token", email="native@example.com")
        manager = AccountManager(str(self.config_file))
        accounts = manager.load_accounts()
        self.assertEqual(accounts[0].config.token, "native-token")
        self.assertEqual(read_cli_auth_token(account_config_dir(accounts[0].home_dir)), "native-token")

    def test_empty_explicit_file_does_not_enable_fallback_account(self):
        self.write_accounts([])
        os.environ["CODEBUFF_API_KEY"] = "host-token"
        self.assertEqual(AccountManager(str(self.config_file)).load_accounts(), [])

    def test_disabled_account_can_keep_an_empty_token_without_seeding(self):
        self.write_accounts([{"id": "disabled", "name": "Disabled", "token": "", "enabled": False}])
        accounts = AccountManager(str(self.config_file)).load_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertFalse(accounts[0].config.enabled)
        self.assertFalse(Path(accounts[0].home_dir).exists())

    def test_invalid_or_duplicate_ids_do_not_replace_active_configuration(self):
        valid = {"id": "one", "name": "One", "token": "token-one"}
        self.write_accounts([valid])
        manager = AccountManager(str(self.config_file))
        manager.load_accounts()
        original = manager.accounts
        for accounts in ([{**valid, "id": "../escape"}], [valid, valid], [valid, {**valid, "id": "ONE"}]):
            self.write_accounts(accounts)
            with self.assertRaises(ValueError):
                manager.load_accounts()
            self.assertIs(manager.accounts, original)
        self.assertFalse((self.root / "escape").exists())

    def test_missing_token_does_not_overwrite_existing_sandbox_keychain_pointer(self):
        config = Path(account_config_dir(str(self.sandboxes / "default")))
        config.mkdir(parents=True)
        path = config / "credentials.json"
        original = '{"default":{"email":"u@example.com","tokenStore":"keychain"}}'
        path.write_text(original)
        accounts = AccountManager(str(self.config_file)).load_accounts()
        self.assertEqual(accounts[0].config.token, "")
        self.assertEqual(path.read_text(), original)


if __name__ == "__main__":
    unittest.main()
