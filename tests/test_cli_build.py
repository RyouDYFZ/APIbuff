"""Offline regressions for privacy build selection and startup enforcement."""

import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cli_build import AUDITED_BUNDLE_SHA256, PRIVATE_GUARD, sha256_file, verify_private_cli
from app.terminal_env import get_standard_terminal_env
from app.worker import FreebuffWorker
from scripts.build_private_cli import patch_bundle


class PrivateBuildTests(unittest.TestCase):
    def test_receipt_rejects_missing_changed_and_wrong_target_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "freebuff"
            binary.write_bytes(b"offline fixture")
            binary.chmod(0o700)
            receipt = binary.with_name(binary.name + ".privacy.json")
            with self.assertRaisesRegex(RuntimeError, "verified private"):
                verify_private_cli(str(binary))
            metadata = {
                "privacy_build_version": 1,
                "platform": platform.system().lower(),
                "machine": platform.machine(),
                "posthog_switch": "CODEBUFF_DISABLE_POSTHOG",
                "source_bundle_sha256": AUDITED_BUNDLE_SHA256,
                "guard_sha256": sha256_file(PRIVATE_GUARD),
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            }
            receipt.write_text(json.dumps(metadata))
            verify_private_cli(str(binary))
            for key, value in (("machine", "wrong-target"),
                               ("posthog_switch", "unsupported"),
                               ("source_bundle_sha256", "old-bundle"),
                               ("guard_sha256", "old-guard"),
                               ("privacy_build_version", 999)):
                receipt.write_text(json.dumps({**metadata, key: value}))
                with self.subTest(key=key), self.assertRaises(RuntimeError):
                    verify_private_cli(str(binary))
            receipt.write_text(json.dumps(metadata))
            binary.write_bytes(b"replaced by upstream auto-update")
            with self.assertRaises(RuntimeError):
                verify_private_cli(str(binary))

    def test_unknown_bundle_cannot_produce_a_privacy_build(self):
        with self.assertRaisesRegex(ValueError, "Unsupported CLI bundle"):
            patch_bundle(b"unknown upstream release", "")

    def test_custom_environment_cannot_reenable_either_sink(self):
        overrides = {"CODEBUFF_DISABLE_POSTHOG": "false", "CODEBUFF_SHIP_LOGS": "true"}
        with patch.dict(os.environ, overrides):
            env = get_standard_terminal_env(overrides)
        self.assertEqual(env["CODEBUFF_DISABLE_POSTHOG"], "1")
        self.assertEqual(env["CODEBUFF_SHIP_LOGS"], "false")
        self.assertEqual(overrides["CODEBUFF_DISABLE_POSTHOG"], "false")


class PrivateWorkerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_cli_is_rejected_before_credentials_or_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = FreebuffWorker(workdir=str(root / "work"), home_dir=str(root / "home"))
            with patch("app.worker.FREEBUFF_BIN", str(root / "official-cli")), \
                 patch("app.worker.write_cli_credentials") as credentials, \
                 patch("app.worker.asyncio.create_subprocess_exec") as spawn:
                with self.assertRaisesRegex(RuntimeError, "verified private"):
                    await worker.start()
            credentials.assert_not_called()
            spawn.assert_not_called()
            self.assertIsNone(worker.master_fd)
            self.assertIsNone(worker._supervisor)


if __name__ == "__main__":
    unittest.main()
