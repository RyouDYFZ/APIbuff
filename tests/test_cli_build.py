"""Offline regressions for official CLI verification and startup enforcement."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cli_build import (
    OFFICIAL_CHECKSUMS,
    _pinned_binary_digests,
    verify_official_cli,
)
from app.terminal_env import get_standard_terminal_env
from app.worker import FreebuffWorker

# Captured before any test patches the module, so the damaged-registry test
# can exercise the real registry reader against a broken file.
_REAL_PINNED_BINARY_DIGESTS = _pinned_binary_digests


class OfficialCliVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        registry = json.loads(OFFICIAL_CHECKSUMS.read_text(encoding="utf-8"))
        pinned = next(iter(registry["binary_sha256"].values()))
        self.binary = self.root / "freebuff"
        self.binary.write_bytes(b"official release bytes")
        self.binary.chmod(0o700)
        self.digests = frozenset({pinned, hashlib.sha256(b"official release bytes").hexdigest()})
        self.pinned = patch("app.cli_build._pinned_binary_digests", return_value=self.digests)
        self.pinned.start()
        self.addCleanup(self.pinned.stop)

    def test_pinned_binary_passes_and_tampering_fails(self):
        verify_official_cli(str(self.binary))
        self.binary.write_bytes(b"replaced by upstream auto-update")
        with self.assertRaisesRegex(RuntimeError, "pinned official"):
            verify_official_cli(str(self.binary))

    def test_missing_nonexecutable_and_relative_paths_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "not an executable file"):
            verify_official_cli(str(self.root / "absent"))
        self.binary.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "not an executable file"):
            verify_official_cli(str(self.binary))
        self.binary.chmod(0o700)
        with self.assertRaisesRegex(RuntimeError, "not an executable file"):
            verify_official_cli("freebuff")

    def test_unpinned_digest_is_rejected_even_when_a_registry_exists(self):
        self.binary.write_bytes(b"unknown upstream release")
        self.binary.chmod(0o700)
        unknown = patch("app.cli_build._pinned_binary_digests",
                        return_value=frozenset({"0" * 64}))
        with unknown:
            with self.assertRaisesRegex(RuntimeError, "SHA-256 does not match"):
                verify_official_cli(str(self.binary))

    def test_registry_without_binary_section_verifies_nothing(self):
        # A damaged registry must fail closed rather than accept any binary.
        with patch("app.cli_build.OFFICIAL_CHECKSUMS",
                   Path(self.root / "damaged.json")) as damaged, \
             patch("app.cli_build._pinned_binary_digests",
                   _REAL_PINNED_BINARY_DIGESTS):
            damaged.write_text(json.dumps({"sha256": {}}))
            self.assertEqual(_pinned_binary_digests(), frozenset())
            with self.assertRaisesRegex(RuntimeError, "SHA-256 does not match"):
                verify_official_cli(str(self.binary))

    def test_host_environment_cannot_reenable_log_shipping(self):
        overrides = {"CODEBUFF_SHIP_LOGS": "true"}
        with patch.dict(os.environ, overrides):
            env = get_standard_terminal_env(overrides)
        self.assertEqual(env["CODEBUFF_SHIP_LOGS"], "false")
        # The PostHog SDK honours no environment switch; its egress is refused
        # by the gateway proxy, so no PostHog variable is injected at all.
        self.assertNotIn("CODEBUFF_DISABLE_POSTHOG", env)
        self.assertEqual(overrides["CODEBUFF_SHIP_LOGS"], "true")


class OfficialWorkerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_cli_is_rejected_before_credentials_or_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = FreebuffWorker(workdir=str(root / "work"), home_dir=str(root / "home"))
            with patch("app.worker.FREEBUFF_BIN", str(root / "official-cli")), \
                 patch("app.worker.write_cli_credentials") as credentials, \
                 patch("app.worker.asyncio.create_subprocess_exec") as spawn:
                with self.assertRaisesRegex(RuntimeError, "pinned official"):
                    await worker.start()
            credentials.assert_not_called()
            spawn.assert_not_called()
            self.assertIsNone(worker.master_fd)
            self.assertIsNone(worker._supervisor)


if __name__ == "__main__":
    unittest.main()
