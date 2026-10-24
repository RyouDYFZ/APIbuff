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

from app.cli_build import (
    AUDITED_BUNDLE_SHA256,
    PATCHED_FACTORY_PATTERN,
    PRIVATE_GUARD,
    digest_and_embeds_patch,
    sha256_file,
    verify_private_cli,
)
from app.terminal_env import get_standard_terminal_env
from app.worker import FreebuffWorker
from scripts.build_private_cli import patch_bundle

# A minimal bundle body that satisfies the structural factory checks.
FACTORY_SOURCE = (
    "class gJT{constructor(T,A={}){this.options=A}}"
    "function ud(T,A){return new gJT(T,{...A,before_send:LvT()})}"
)
PATCHED_BODY = b"if (gatewayPostHogDisabled())\n    return gatewayDisabledPostHogClient();\n"


class PrivateBuildTests(unittest.TestCase):
    def write_fixture(self, temporary, body=b"offline fixture\n" + PATCHED_BODY):
        binary = Path(temporary) / "freebuff"
        binary.write_bytes(body)
        binary.chmod(0o700)
        return binary

    def receipt_for(self, binary, **overrides):
        metadata = {
            "privacy_build_version": 1,
            "platform": platform.system().lower(),
            "machine": platform.machine(),
            "posthog_switch": "CODEBUFF_DISABLE_POSTHOG",
            "source_bundle_sha256": AUDITED_BUNDLE_SHA256,
            "guard_sha256": sha256_file(PRIVATE_GUARD),
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        }
        metadata.update(overrides)
        binary.with_name(binary.name + ".privacy.json").write_text(json.dumps(metadata))
        return metadata

    def test_receipt_rejects_missing_changed_and_wrong_target_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = self.write_fixture(temporary)
            with self.assertRaisesRegex(RuntimeError, "verified private"):
                verify_private_cli(str(binary))
            self.receipt_for(binary)
            verify_private_cli(str(binary))
            for key, value in (("machine", "wrong-target"),
                               ("posthog_switch", "unsupported"),
                               ("source_bundle_sha256", "old-bundle"),
                               ("guard_sha256", "old-guard"),
                               ("privacy_build_version", 999)):
                self.receipt_for(binary, **{key: value})
                with self.subTest(key=key), self.assertRaises(RuntimeError):
                    verify_private_cli(str(binary))
            self.receipt_for(binary)
            binary.write_bytes(b"replaced by upstream auto-update")
            with self.assertRaises(RuntimeError):
                verify_private_cli(str(binary))

    def test_forged_receipt_cannot_whitelist_an_official_binary(self):
        """A receipt generated for an official binary must never verify."""
        with tempfile.TemporaryDirectory() as temporary:
            # An official binary never embeds the guard functions.
            binary = self.write_fixture(temporary, body=b"official release bytes\n")
            self.receipt_for(binary)
            with self.assertRaisesRegex(RuntimeError, "verified private"):
                verify_private_cli(str(binary))

    def test_patch_marker_tolerates_compiler_formatting(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = self.write_fixture(temporary, body=b"prefix\n" + PATCHED_BODY + b"suffix\n")
            digest, embedded = digest_and_embeds_patch(binary)
            self.assertTrue(embedded)
            self.assertEqual(digest, hashlib.sha256(binary.read_bytes()).hexdigest())
            self.assertIsNotNone(PATCHED_FACTORY_PATTERN.search(PATCHED_BODY))

    def test_unknown_bundle_cannot_produce_a_privacy_build(self):
        with self.assertRaisesRegex(ValueError, "coverage has changed"):
            patch_bundle(b"unknown upstream release", "")

    def test_patch_bundle_inserts_the_guard_at_the_unique_factory(self):
        source = patch_bundle(FACTORY_SOURCE.encode("utf-8"), "// guard\n")
        self.assertIn("// guard\n", source)
        self.assertIn("if(gatewayPostHogDisabled())return gatewayDisabledPostHogClient()", source)
        self.assertIn("return new gJT(T,{...A,before_send:LvT()})", source)
        self.assertEqual(source.count("new gJT("), 1)

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
