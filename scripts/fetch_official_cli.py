#!/usr/bin/env python3
"""Fetch the pinned official Freebuff CLI and install it for gateway workers.

The gateway runs the official executable byte-for-byte — it is never rebuilt,
repacked or modified. Telemetry is blocked at the gateway proxy, so the only
provenance question is whether the binary on disk is exactly the pinned
release. This script answers it fail-closed:

- ``--fetch`` downloads ``freebuff-<target>.tar.gz`` from the fixed https
  origin (redirects confined to the publisher's own hosts), and ``--archive``
  accepts an already-downloaded tarball.
- The archive's SHA-256 must match ``cli/freebuff-0.2.34-binary-checksums.json``
  (the npm package's binaryChecksums, a trust root separate from this
  repository), and the extracted executable's SHA-256 must match the same
  registry's ``binary_sha256`` section before anything is installed.

Upgrades are therefore explicit: a new official release must be audited and
its digests pinned in the registry before this script will install it.
"""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.cli_build import (  # noqa: E402
    OFFICIAL_CHECKSUMS,
    sha256_file,
)

FREEBUFF_VERSION = "0.2.34"
# Fixed https origin; release archives are fetched from nowhere else.
RELEASE_HOST = "codebuff.com"

TARGETS = {
    "darwin-arm64": {"system": "Darwin", "machine": "arm64"},
    "linux-x64": {"system": "Linux", "machine": "x86_64"},
    "linux-arm64": {"system": "Linux", "machine": "aarch64"},
}

# The publisher serves releases through its own GitHub repository and GitHub's
# asset CDN; redirects are confined to these hosts plus the pinned origin.
# The downloaded archive is still worthless to an attacker: its SHA-256 must
# match the committed registry before anything is extracted or installed.
RELEASE_HOSTS = frozenset({
    "codebuff.com", "www.codebuff.com",
    "freebuff.com", "www.freebuff.com",
    "github.com", "www.github.com",
    "objects.githubusercontent.com", "release-assets.githubusercontent.com",
})
# Redirects into GitHub must stay inside the publisher's release repository.
GITHUB_RELEASE_PATH = "/CodebuffAI/codebuff-community/releases/download/"


class _ControlledRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if (req.type != "https" or target.scheme != "https"
                or target.hostname not in RELEASE_HOSTS
                or (target.hostname in ("github.com", "www.github.com")
                    and not target.path.startswith(GITHUB_RELEASE_PATH))):
            raise ValueError(f"Release download redirect to {newurl} is not accepted")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Local VPN clients in fake-ip DNS mode answer every name from this benchmark
# range; that is the operator's proxy egress, not an internal service.
_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def _is_blocked_address(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    return (ip.is_loopback or ip.is_link_local or ip.is_private) and ip not in _FAKE_IP_NETWORK


def current_target() -> str:
    for name, expect in TARGETS.items():
        if (platform.system(), platform.machine()) == (expect["system"], expect["machine"]):
            return name
    return ""


def safe_absolute(path: Path, what: str) -> Path:
    """Accept only operator-supplied absolute filesystem paths."""
    resolved = Path(path).expanduser()
    if not resolved.is_absolute() or "\0" in str(resolved):
        raise ValueError(f"{what} must be an absolute path: {path}")
    return resolved


def require_file(path: Path, what: str) -> Path:
    resolved = safe_absolute(path, what)
    if not resolved.is_file():
        raise ValueError(f"{what} must be an existing file: {path}")
    return resolved


def load_registry() -> dict:
    registry = json.loads(OFFICIAL_CHECKSUMS.read_text(encoding="utf-8"))
    if registry.get("version") != FREEBUFF_VERSION:
        raise ValueError("Official checksum registry version mismatch")
    for section in ("sha256", "binary_sha256"):
        if not isinstance(registry.get(section), dict) or not registry[section]:
            raise ValueError(f"Official checksum registry section '{section}' is missing")
    return registry


def fetch_release_archive(target: str, destination: Path) -> Path:
    """Download the pinned release over https from the fixed public hosts only."""
    url = f"https://{RELEASE_HOST}/api/releases/download/{FREEBUFF_VERSION}/freebuff-{target}.tar.gz"
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in RELEASE_HOSTS:
        raise ValueError("Release downloads are restricted to the pinned https origin")
    # Resolve now and refuse loopback/private/link-local targets (SSRF guard).
    for _, _, _, _, sockaddr in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM):
        if _is_blocked_address(ipaddress.ip_address(sockaddr[0])):
            raise ValueError("Release host does not resolve to a public address")
    request = urllib.request.Request(url, headers={"User-Agent": "freebuff-official-cli-fetch/1.0"})
    opener = urllib.request.build_opener(_ControlledRedirects)
    with opener.open(request, timeout=300) as response, destination.open("wb") as stream:
        shutil.copyfileobj(response, stream)
    return destination


def extract_official_binary(archive: Path, work: Path) -> Path:
    with tarfile.open(require_file(archive, "release archive"), "r:gz") as bundle:
        members = [m for m in bundle.getmembers() if m.isfile() and Path(m.name).name == "freebuff"]
        if not members:
            raise ValueError("Release archive does not contain the freebuff executable")
        binary = work / "official" / "freebuff"
        binary.parent.mkdir(parents=True, exist_ok=True)
        with binary.open("wb") as stream:
            extracted = bundle.extractfile(members[0])
            if extracted is None:
                raise ValueError("Release archive member could not be read")
            shutil.copyfileobj(extracted, stream)
    return binary


def install(target: str, archive: Path, output: Path, registry: dict) -> None:
    if output.name != "freebuff":
        raise ValueError("The official CLI must be installed as a file named 'freebuff'")
    if output.exists():
        raise ValueError("Output already exists; choose a new output path to preserve that file")
    expected_archive = registry["sha256"].get(target) or registry["sha256"].get(f"{target}-baseline")
    actual_archive = sha256_file(require_file(archive, "release archive"))
    if not expected_archive or actual_archive != expected_archive:
        raise ValueError(
            f"Refusing unpinned official archive for {target}: its SHA-256 does not "
            f"match {OFFICIAL_CHECKSUMS.name}. Obtain freebuff-{FREEBUFF_VERSION} "
            "exactly; a different release must be audited and pinned first."
        )

    with tempfile.TemporaryDirectory(prefix="freebuff-official-cli-") as temporary:
        binary = extract_official_binary(archive, Path(temporary))
        expected_binary = registry["binary_sha256"].get(target)
        actual_binary = sha256_file(binary)
        if not expected_binary or actual_binary != expected_binary:
            raise ValueError(
                f"Refusing unpinned official executable for {target}: the extracted "
                f"binary's SHA-256 does not match {OFFICIAL_CHECKSUMS.name}. The "
                "release must be audited and pinned before workers may run it."
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_output = tempfile.mkstemp(prefix=output.name + ".", dir=output.parent)
        staged = Path(temporary_output)
        try:
            with os.fdopen(descriptor, "wb") as stream, binary.open("rb") as source:
                shutil.copyfileobj(source, stream)
            staged.chmod(0o755)
            # Atomic installation without replacing a concurrently created file.
            os.link(staged, output)
        finally:
            staged.unlink(missing_ok=True)
    print(f"Installed official CLI {FREEBUFF_VERSION} ({target}): {output}")
    print(f"archive sha256: {actual_archive}")
    print(f"binary sha256: {actual_binary}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=sorted(TARGETS), default=current_target(),
                        help="Release target (default: this machine)")
    parser.add_argument("--archive", type=Path,
                        help=f"Official freebuff-{FREEBUFF_VERSION} release tarball for --target")
    parser.add_argument("--fetch", action="store_true",
                        help=f"Download the release tarball for --target from https://{RELEASE_HOST}")
    parser.add_argument("--output", type=Path, default=PROJECT / ".freebuff" / "official-cli" / "freebuff",
                        help="Installation path (default: .freebuff/official-cli/freebuff)")
    args = parser.parse_args()
    if not args.target:
        parser.error("This machine is not a supported release target; pass --target")
    if bool(args.archive) == bool(args.fetch):
        parser.error("Choose exactly one source: --archive or --fetch")
    try:
        registry = load_registry()
        if args.archive:
            install(args.target, safe_absolute(args.archive, "release archive"), args.output, registry)
        else:
            with tempfile.TemporaryDirectory(prefix="freebuff-release-") as temporary:
                downloaded = fetch_release_archive(args.target, Path(temporary) / "release.tar.gz")
                install(args.target, downloaded, args.output, registry)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Official CLI install failed: {error}\n")


if __name__ == "__main__":
    main()
