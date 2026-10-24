"""Verify the private CLI build before a worker can use account credentials."""

import hashlib
import json
import os
from pathlib import Path
import platform

PRIVATE_CLI = Path(__file__).resolve().parents[1] / ".freebuff" / "private-cli" / "freebuff"
PRIVATE_GUARD = Path(__file__).resolve().parents[1] / "cli" / "posthog-opt-out.js"
AUDITED_BUNDLE_SHA256 = "c19723acf4d76aee80d2da6f31ee1d5850d255e42f6e32a462612e44248cbadf"
PRIVACY_BUILD_VERSION = 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_private_cli(binary: str) -> None:
    """Fail closed on an official/replaced binary that would ignore our flag.

    The sidecar is a local build receipt, not a publisher signature or a
    defence against an attacker who can replace both files.
    """
    path = Path(binary)
    receipt = path.with_name(path.name + ".privacy.json")
    try:
        metadata = json.loads(receipt.read_text())
        valid = (
            path.is_absolute()
            and path.is_file()
            and os.access(path, os.X_OK)
            and metadata["privacy_build_version"] == PRIVACY_BUILD_VERSION
            and metadata["platform"] == platform.system().lower()
            and metadata["machine"] == platform.machine()
            and metadata["posthog_switch"] == "CODEBUFF_DISABLE_POSTHOG"
            and metadata["source_bundle_sha256"] == AUDITED_BUNDLE_SHA256
            and metadata["guard_sha256"] == sha256_file(PRIVATE_GUARD)
            and metadata["binary_sha256"] == sha256_file(path)
        )
    except (OSError, ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise RuntimeError(
            "A verified private Freebuff CLI build is required to disable PostHog. "
            "Run scripts/build_private_cli.py for this platform and set FREEBUFF_BIN "
            "to that build; the official binary does not support the opt-out switch."
        )
