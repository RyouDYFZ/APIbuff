"""Offline entrypoint and explicit-account credential regression checks."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.accounts import AccountManager


def write_official_cli_fixture(root: Path) -> Path:
    """A stand-in executable; entrypoint verification is stubbed by the
    python3 wrapper below because the real check pins the release digest."""
    binary = root / "official-cli" / "freebuff"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"official release bytes\n")
    binary.chmod(0o700)
    return binary


class TestContainerCredentialStartup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.native = self.root / "home" / ".config" / "manicode"
        self.native.mkdir(parents=True)
        (self.native / "freebuff").touch()  # No binary download or live CLI invocation.
        self.config = self.root / "account-config"
        self.config.mkdir()
        self.workspace = self.root / "workspace"
        (self.workspace / ".git").mkdir(parents=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        python_wrapper = bin_dir / "python3"
        python_wrapper.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = "-m" ] && [ "$2" = "uvicorn" ]; then\n'
            '  echo "TEST_SERVER_DISPATCHED"\n'
            '  exit 0\n'
            'fi\n'
            # The pinned-digest verification cannot pass against an offline
            # fixture, so this wrapper stands in for exactly that heredoc;
            # every other stdin script still runs under the real interpreter.
            'if [ "$1" = "-" ]; then\n'
            '  script=$(cat)\n'
            '  case "$script" in\n'
            '    *verify_official_cli*)\n'
            '      echo "Verified pinned official Freebuff CLI: test fixture"\n'
            '      exit 0\n'
            '      ;;\n'
            '  esac\n'
            f'  printf \'%s\' "$script" | exec {shlex.quote(sys.executable)} -\n'
            'fi\n'
            f'exec {shlex.quote(sys.executable)} "$@"\n'
        )
        python_wrapper.chmod(0o700)
        self.env = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "FREEBUFF_CONFIG_DIR": str(self.config),
            "FREEBUFF_WORKER_WORKDIR": str(self.workspace),
            "FREEBUFF_BIN": str(write_official_cli_fixture(self.root)),
            "PATH": str(bin_dir) + ":/usr/bin:/bin",
        }
        for key in ("CODEBUFF_API_KEY", "FREEBUFF_AUTH_TOKEN", "FREEBUFF_USER_EMAIL"):
            self.env.pop(key, None)
        self.credentials = self.config / "credentials.json"
        self.settings = self.config / "settings.json"
        self.credentials.write_text(json.dumps({
            "default": {"authToken": "old-token", "email": "old@example.com", "name": "Existing Name", "tokenStore": "file"},
            "another-profile": {"preserved": True},
        }))
        self.settings.write_text('{"adsEnabled":false,"freebuffModelKey":"chosen-model"}')

    def run_entrypoint(self):
        result = subprocess.run(
            ["/bin/bash", str(ROOT / "entrypoint.sh")],
            cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TEST_SERVER_DISPATCHED", result.stdout)
        return result

    def test_token_seeding_preserves_settings_and_metadata(self):
        token = 'token-"quote\\slash\nline-$(printf SHOULD_STAY_LITERAL)'
        self.env["CODEBUFF_API_KEY"] = token
        original_settings = self.settings.read_bytes()
        result = self.run_entrypoint()
        data = json.loads(self.credentials.read_text())
        self.assertEqual(data["default"]["authToken"], token)
        self.assertEqual(data["default"]["email"], "old@example.com")
        self.assertEqual(data["default"]["name"], "Existing Name")
        self.assertNotIn("tokenStore", data["default"])
        self.assertEqual(data["another-profile"], {"preserved": True})
        self.assertEqual(self.settings.read_bytes(), original_settings)
        self.assertFalse((self.native / "credentials.json").exists())
        self.assertNotIn(token, result.stdout + result.stderr)

    def test_no_token_preserves_credentials_and_settings(self):
        original_credentials = self.credentials.read_bytes()
        original_settings = self.settings.read_bytes()
        self.run_entrypoint()
        self.assertEqual(self.credentials.read_bytes(), original_credentials)
        self.assertEqual(self.settings.read_bytes(), original_settings)

    def test_explicit_accounts_reject_missing_tokens_without_reusing_old_login(self):
        config_file = self.root / "accounts.json"
        valid = {"id": "one", "name": "One", "token": "old-account-token"}
        config_file.write_text(json.dumps({"accounts": [valid]}))
        with patch("app.accounts.BASE_SANDBOX_DIR", str(self.root / "sandboxes")), patch("app.accounts.global_proxy_manager") as proxy:
            proxy.nodes = []
            proxy.singbox_bin = None
            manager = AccountManager(str(config_file))
            manager.load_accounts()
            original_accounts = manager.accounts
            for token in ("", "  ", "unauthenticated"):
                with self.subTest(token=token):
                    config_file.write_text(json.dumps({"accounts": [{**valid, "token": token}]}))
                    with self.assertRaises(ValueError):
                        manager.load_accounts()
                    self.assertIs(manager.accounts, original_accounts)


if __name__ == "__main__":
    unittest.main()
