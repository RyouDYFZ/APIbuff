"""Verify the official CLI binary before a worker can use account credentials.

The gateway runs the pinned official Freebuff executable byte-for-byte: it is
never rebuilt or repackaged. Privacy is enforced at the network edge instead —
the gateway's sing-box daemon refuses every telemetry request (see
``app/proxy_manager``) — so the only remaining trust question is whether the
binary on disk is exactly the audited official release. ``verify_official_cli``
answers it by SHA-256 against the pinned registry, and workers refuse to start
(never touching account credentials) unless it passes.
"""

import hashlib
import json
import os
from pathlib import Path

OFFICIAL_CLI = Path(__file__).resolve().parents[1] / ".freebuff" / "official-cli" / "freebuff"
OFFICIAL_CLI_VERSION = "0.2.34"
# SHA-256 of the official executable per release target, mirrored from the
# pinned release archives whose own digests come from the npm package's
# binaryChecksums (a trust root separate from this repository).
OFFICIAL_CHECKSUMS = Path(__file__).resolve().parents[1] / "cli" / "freebuff-0.2.34-binary-checksums.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pinned_binary_digests() -> "frozenset[str]":
    """Executable digests of every pinned official release target."""
    try:
        registry = json.loads(OFFICIAL_CHECKSUMS.read_text(encoding="utf-8"))
        binaries = registry.get("binary_sha256")
        values = binaries.values() if isinstance(binaries, dict) else []
        return frozenset(
            value for value in values if isinstance(value, str) and len(value) == 64
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return frozenset()


def verify_official_cli(binary: str) -> None:
    """Fail closed unless the binary is the pinned official release."""
    path = Path(binary)
    if not (path.is_absolute() and path.is_file() and os.access(path, os.X_OK)):
        raise RuntimeError(
            "A pinned official Freebuff CLI is required before workers may use "
            "account credentials, and the configured FREEBUFF_BIN is not an "
            "executable file. Obtain the official release with "
            "scripts/fetch_official_cli.py and point FREEBUFF_BIN at it."
        )
    digest = sha256_file(path)
    if digest not in _pinned_binary_digests():
        raise RuntimeError(
            "A pinned official Freebuff CLI is required before workers may use "
            "account credentials: the binary's SHA-256 does not match "
            f"{OFFICIAL_CHECKSUMS.name}. Telemetry is blocked at the gateway "
            "proxy rather than by modifying the executable, so only the exact "
            "audited official release may run. Re-run scripts/fetch_official_cli.py "
            "or audit and pin the new release first."
        )
