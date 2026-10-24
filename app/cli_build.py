"""Verify the private CLI build before a worker can use account credentials."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re

PRIVATE_CLI = Path(__file__).resolve().parents[1] / ".freebuff" / "private-cli" / "freebuff"
PRIVATE_GUARD = Path(__file__).resolve().parents[1] / "cli" / "posthog-opt-out.js"
# SHA-256 of every official 0.2.34 release archive and the extracted
# JavaScript bundle digest per target, mirrored from the npm package's
# binaryChecksums (a trust root separate from this repository). A receipt
# built from the pinned release pins its JavaScript transitively.
OFFICIAL_CHECKSUMS = Path(__file__).resolve().parents[1] / "cli" / "freebuff-0.2.34-binary-checksums.json"
AUDITED_BUNDLE_SHA256 = "c19723acf4d76aee80d2da6f31ee1d5850d255e42f6e32a462612e44248cbadf"
PRIVACY_BUILD_VERSION = 1

# Emitted into every private build by scripts/build_private_cli.py: the PostHog
# factory is rewritten to consult the guard first. The two guard function names
# exist only in our patched bundle, so an official binary cannot match, and the
# whitespace-tolerant shape survives the compiler's reprinting. This pattern is
# what makes the receipt meaningful: a receipt forged against an official
# binary (its own digest, copied constants) still fails verification.
PATCHED_FACTORY_PATTERN = re.compile(
    rb"if\s*\(\s*gatewayPostHogDisabled\s*\(\s*\)\s*\)\s*return\s+gatewayDisabledPostHogClient\s*\(\s*\)\s*;"
)
_PATTERN_WINDOW = 512


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_and_embeds_patch(path: Path) -> "tuple[str, bool]":
    """One streaming pass: the file digest and whether the opt-out patch is inside."""
    digest = hashlib.sha256()
    embedded = False
    tail = b""
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
            if not embedded:
                window = tail + chunk
                if PATCHED_FACTORY_PATTERN.search(window):
                    embedded = True
                tail = window[-_PATTERN_WINDOW:]
    return digest.hexdigest(), embedded


def _official_binary_digests() -> "frozenset[str]":
    try:
        registry = json.loads(OFFICIAL_CHECKSUMS.read_text(encoding="utf-8"))
        values = registry["sha256"].values() if isinstance(registry, dict) else []
        return frozenset(
            value for value in values if isinstance(value, str) and len(value) == 64
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return frozenset()


def _pinned_bundle_digests() -> "frozenset[str]":
    """JavaScript bundle digests of every pinned official release target."""
    try:
        registry = json.loads(OFFICIAL_CHECKSUMS.read_text(encoding="utf-8"))
        bundles = registry.get("bundle_sha256")
        values = bundles.values() if isinstance(bundles, dict) else []
        return frozenset(
            value for value in values if isinstance(value, str) and len(value) == 64
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return frozenset()


def verify_private_cli(binary: str) -> None:
    """Fail closed on an official or replaced binary, with or without a receipt."""
    path = Path(binary)
    receipt = path.with_name(path.name + ".privacy.json")
    try:
        metadata = json.loads(receipt.read_text(encoding="utf-8"))
        source_digest = metadata["source_bundle_sha256"]
        source_ok = (
            source_digest == AUDITED_BUNDLE_SHA256
            or source_digest in _pinned_bundle_digests()
            or (
                isinstance(metadata.get("official_archive_sha256"), str)
                and metadata["official_archive_sha256"] in _official_binary_digests()
            )
        )
        valid = (
            path.is_absolute()
            and path.is_file()
            and os.access(path, os.X_OK)
            and metadata["privacy_build_version"] == PRIVACY_BUILD_VERSION
            and metadata["platform"] == platform.system().lower()
            and metadata["machine"] == platform.machine()
            and metadata["posthog_switch"] == "CODEBUFF_DISABLE_POSTHOG"
            and source_ok
            and metadata["guard_sha256"] == sha256_file(PRIVATE_GUARD)
        )
    except (OSError, ValueError, KeyError, TypeError):
        valid = False
    if valid:
        try:
            binary_sha256, embedded = digest_and_embeds_patch(path)
        except OSError:
            valid = False
        else:
            # The patch marker plus the digest bind this exact binary to our
            # build pipeline; neither alone is trustworthy.
            valid = embedded and binary_sha256 == metadata["binary_sha256"]
    if not valid:
        raise RuntimeError(
            "A verified private Freebuff CLI build is required to disable PostHog: "
            "the binary must embed the PostHog opt-out patch and carry its build "
            "receipt. Run scripts/build_private_cli.py for this platform and set "
            "FREEBUFF_BIN to that build. The official binary is rejected even "
            "when a receipt was generated for it."
        )
