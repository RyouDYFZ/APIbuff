#!/usr/bin/env python3
"""Rebuild the pinned Freebuff 0.2.34 CLI with a runtime PostHog opt-out.

Provenance modes, both fail-closed:

- ``--archive PATH`` / ``--fetch`` extracts the pinned release from the
  official ``freebuff-<target>.tar.gz`` whose SHA-256 must match
  ``cli/freebuff-0.2.34-binary-checksums.json`` (the launcher's own trust
  root). The extracted JavaScript must match the per-target bundle digest in
  the same registry, and every embedded asset must match the per-target
  pinned asset registry. This is the recommended mode for every platform and
  the only mode for Linux targets.
- ``--extracted DIR`` rebuilds the audited macOS bundle in ``extracted_sources``
  whose JavaScript must hash to the audited digest (development convenience).

The PostHog client factory is located structurally (minifier names differ per
target), its constructor must be unique, and the constructed class must be the
PostHog client. Coverage that changed in any way refuses the build.

Never executes the CLI, overwrites an installed official binary, or downloads
dependencies. Compilation itself lives in ``private_cli_compiler``, whose
subprocess argv is a per-target constant; this script passes it only a private
temporary working directory.
"""

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import socket
import subprocess  # exception types only; no subprocess is spawned here
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.cli_build import (  # noqa: E402
    PRIVATE_CLI,
    PRIVACY_BUILD_VERSION,
    OFFICIAL_CHECKSUMS,
    sha256_file,
)
from scripts.private_cli_compiler import TARGET_ASSETS, compile_private_cli  # noqa: E402

FREEBUFF_VERSION = "0.2.34"
# Fixed https origin; release archives are fetched from nowhere else.
RELEASE_HOST = "codebuff.com"
UNBUNED = PROJECT / "unbuned" / "unbuned.py"
ASSET_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

TARGETS = {
    "darwin-arm64": {"system": "Darwin", "machine": "arm64",
                     "assets": "assets-0.2.34-darwin-arm64.json"},
    "linux-x64": {"system": "Linux", "machine": "x86_64",
                  "assets": "assets-0.2.34-linux-x64.json"},
    "linux-arm64": {"system": "Linux", "machine": "aarch64",
                    "assets": "assets-0.2.34-linux-arm64.json"},
}

# Structural shape of the PostHog client factory across minifier outputs:
#   function NAME(a,b){return new CTOR(a,{...b,before_send:HOOK()})}
# The patch keeps every original identifier and only inserts the guard.
FACTORY_PATTERN = re.compile(
    r"function ([A-Za-z_$][\w$]*)\(([A-Za-z_$][\w$]*),([A-Za-z_$][\w$]*)\)"
    r"\{return new ([A-Za-z_$][\w$]*)\(\2,\{\.\.\.\3,before_send:([A-Za-z_$][\w$]*)\(\)\}\)\}"
)
PATCHED_FACTORY_TEMPLATE = (
    "function {name}({left},{right}){{"
    "if(gatewayPostHogDisabled())return gatewayDisabledPostHogClient();"
    "return new {ctor}({left},{{...{right},before_send:{hook}()}})}}"
)


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
    for section in ("sha256", "bundle_sha256"):
        if not isinstance(registry.get(section), dict) or not registry[section]:
            raise ValueError(f"Official checksum registry section '{section}' is missing")
    return registry


def verify_pinned_archive(archive: Path, target: str, expected_sha: str) -> str:
    actual = sha256_file(require_file(archive, "release archive"))
    if not expected_sha or actual != expected_sha:
        raise ValueError(
            f"Refusing unpinned official archive for {target}: its SHA-256 does not "
            f"match {OFFICIAL_CHECKSUMS.name}. Obtain freebuff-{FREEBUFF_VERSION} "
            "exactly; a different release must be audited before it can be rebuilt."
        )
    return actual


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Release download redirects are not accepted")


def fetch_release_archive(target: str, destination: Path) -> Path:
    """Download the pinned release over https from the fixed public host only."""
    url = f"https://{RELEASE_HOST}/api/releases/download/{FREEBUFF_VERSION}/freebuff-{target}.tar.gz"
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != RELEASE_HOST:
        raise ValueError("Release downloads are restricted to the pinned https origin")
    # Resolve now and refuse loopback/private/link-local targets (SSRF guard).
    for _, _, _, _, sockaddr in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(sockaddr[0]).is_global:
            raise ValueError("Release host does not resolve to a public address")
    request = urllib.request.Request(url, headers={"User-Agent": "freebuff-private-cli-build/1.0"})
    opener = urllib.request.build_opener(_NoRedirects)
    with opener.open(request, timeout=300) as response, destination.open("wb") as stream:
        shutil.copyfileobj(response, stream)
    return destination


def binary_from_archive(archive: Path, work: Path) -> Path:
    with tarfile.open(require_file(archive, "release archive"), "r:gz") as bundle:
        members = [m for m in bundle.getmembers() if m.isfile() and Path(m.name).name == "freebuff"]
        if not members:
            raise ValueError("Release archive does not contain the freebuff executable")
        # The extractor names its JavaScript output after the binary; keep the
        # canonical name so extraction always produces freebuff.js.
        binary = work / "official" / "freebuff"
        binary.parent.mkdir(parents=True, exist_ok=True)
        with binary.open("wb") as stream:
            extracted = bundle.extractfile(members[0])
            if extracted is None:
                raise ValueError("Release archive member could not be read")
            shutil.copyfileobj(extracted, stream)
    binary.chmod(0o700)
    return binary


def load_extractor():
    """Import the committed unbuned extractor as a module (never a subprocess)."""
    source = require_file(UNBUNED, "unbuned extractor")
    spec = importlib.util.spec_from_file_location("freebuff_unbuned", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def extract_bundle(binary: Path, work: Path) -> Path:
    """Run the committed unbuned extractor in-process; it never executes the binary."""
    output = work / "extracted"
    try:
        load_extractor().main([
            str(require_file(binary, "official binary")), "-o", str(output), "--assets", "-q"])
    except SystemExit as exit_request:  # unbuned's argparse exits on success too.
        if exit_request.code not in (0, None):
            raise ValueError("Bundle extraction failed") from exit_request
    for required in ("extracted/freebuff.js", "extracted/assets/assets.json"):
        if not (work / required).is_file():
            raise ValueError(f"Extraction did not produce {required}")
    return output


def stage_assets(target: str, extracted: Path, staging: Path) -> None:
    """Validate every embedded asset against the per-target pinned registry."""
    names = TARGET_ASSETS[target]
    registry_path = PROJECT / "cli" / TARGETS[target]["assets"]
    pinned = json.loads(registry_path.read_text(encoding="utf-8"))
    if sorted(pinned) != sorted(names) or len(pinned) != len(names):
        raise ValueError(f"Pinned asset registry {registry_path.name} disagrees with its constant")
    manifest = json.loads((extracted / "assets" / "assets.json").read_text(encoding="utf-8"))
    entries = manifest.get("assets")
    if not isinstance(entries, list) or len(entries) != len(names):
        raise ValueError(f"Expected the complete set of {len(names)} embedded CLI assets")
    seen = set()
    for entry in entries:
        name = PurePosixPath(entry["name"] or "")
        if (str(name.parent) != "/$bunfs/root" or not name.name
                or name.name != Path(name.name).name
                or not ASSET_NAME_PATTERN.fullmatch(name.name)):
            raise ValueError("Unexpected embedded asset path")
        data = (extracted / entry["file"]).read_bytes()
        # The extractor manifest records sizes; digests are pinned per target.
        if len(data) != entry["bytes"]:
            raise ValueError("Extracted asset does not match its manifest record")
        digest = hashlib.sha256(data).hexdigest()
        if pinned.get(name.name) != digest:
            raise ValueError(f"Asset digest differs from the pinned {FREEBUFF_VERSION} set: {name.name}")
        target_file = staging / name.name
        if target_file.exists():
            raise ValueError("Duplicate embedded asset name")
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_bytes(data)
        seen.add(name.name)
    if seen != set(names):
        raise ValueError("Extracted assets do not cover the pinned registry")


def patch_bundle(bundle: bytes, guard: str) -> str:
    """Insert the runtime opt-out guard at the unique PostHog client factory."""
    source = bundle.decode("utf-8")
    matches = list(FACTORY_PATTERN.finditer(source))
    if len(matches) != 1:
        raise ValueError("PostHog factory coverage has changed: expected exactly one factory")
    match = matches[0]
    ctor = match.group(4)
    if source.count(f"new {ctor}(") != 1:
        raise ValueError("PostHog constructor coverage has changed")
    patched = PATCHED_FACTORY_TEMPLATE.format(
        name=match.group(1), left=match.group(2), right=match.group(3),
        ctor=ctor, hook=match.group(5))
    return guard + "\n" + source[:match.start()] + patched + source[match.end():]


def build(target: str, bundle_js: Path, asset_dir: Path, output: Path,
          expected_bundle_sha256: str, official_archive_sha256: str = None) -> None:
    expect = TARGETS[target]
    if (platform.system(), platform.machine()) != (expect["system"], expect["machine"]):
        raise ValueError(f"Target {target} must be built on {expect['system']}/{expect['machine']}")
    if not shutil.which("bun"):
        raise ValueError("Bun is required on PATH to compile the private CLI")
    bundle = bundle_js.read_bytes()
    bundle_sha256 = hashlib.sha256(bundle).hexdigest()
    if bundle_sha256 != expected_bundle_sha256:
        raise ValueError(
            "Unsupported CLI bundle: its JavaScript digest is not the pinned "
            f"{FREEBUFF_VERSION} value for {target}; audit it before rebuilding."
        )
    guard = require_file(PROJECT / "cli" / "posthog-opt-out.js", "PostHog guard").read_bytes()
    source = patch_bundle(bundle, guard.decode("utf-8"))
    output = safe_absolute(output, "output")
    if output.exists() or output.with_name(output.name + ".privacy.json").exists():
        raise ValueError("Output already exists; choose a new output path to preserve that build")

    with tempfile.TemporaryDirectory(prefix="freebuff-private-build-") as temporary:
        work = Path(temporary)
        # The compiler sees only this private directory: the patched
        # entrypoint, the staged assets, and the produced binary.
        (work / "freebuff.js").write_text(source)
        for name in TARGET_ASSETS[target]:
            shutil.copyfile(asset_dir / name, work / name)
        binary = compile_private_cli(target, work)
        metadata = {
            "privacy_build_version": PRIVACY_BUILD_VERSION,
            "cli_version": FREEBUFF_VERSION,
            "platform": expect["system"].lower(),
            "machine": expect["machine"],
            "target": target,
            "posthog_switch": "CODEBUFF_DISABLE_POSTHOG",
            "source_bundle_sha256": bundle_sha256,
            "guard_sha256": hashlib.sha256(guard).hexdigest(),
            "binary_sha256": sha256_file(binary),
            "assets": list(TARGET_ASSETS[target]),
        }
        if official_archive_sha256:
            metadata["official_archive_sha256"] = official_archive_sha256
        # Install the finished build with plain file operations only.
        descriptor, temporary_output = tempfile.mkstemp(prefix=output.name + ".", dir=output.parent)
        staged = Path(temporary_output)
        try:
            with os.fdopen(descriptor, "wb") as stream, binary.open("rb") as compiled:
                shutil.copyfileobj(compiled, stream)
            staged.chmod(0o755)
            # Atomic installation without replacing a concurrently created file.
            os.link(staged, output)
        finally:
            staged.unlink(missing_ok=True)
        with output.with_name(output.name + ".privacy.json").open("x") as receipt:
            receipt.write(json.dumps(metadata, indent=2) + "\n")
    print(f"Built private CLI ({target}): {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=sorted(TARGETS), default=current_target(),
                        help="Build target (default: this machine)")
    parser.add_argument("--extracted", type=Path, default=PROJECT / "extracted_sources",
                        help="Audited extraction directory (JS + assets/), macOS arm64 only")
    parser.add_argument("--archive", type=Path,
                        help=f"Official freebuff-{FREEBUFF_VERSION} release tarball for --target")
    parser.add_argument("--fetch", action="store_true",
                        help=f"Download the release tarball for --target from https://{RELEASE_HOST}")
    parser.add_argument("--output", type=Path, default=PRIVATE_CLI)
    args = parser.parse_args()
    if not args.target:
        parser.error("This machine is not a supported build target; pass --target")
    if args.archive and args.fetch:
        parser.error("Choose one source: --archive or --fetch")
    if not args.archive and not args.fetch and args.target != "darwin-arm64":
        parser.error(f"--extracted only supports the audited macOS bundle; use --archive or --fetch for {args.target}")
    try:
        registry = load_registry()
        with tempfile.TemporaryDirectory(prefix="freebuff-private-src-") as temporary:
            work = Path(temporary)
            official_sha = None
            if args.archive or args.fetch:
                archive = safe_absolute(
                    args.archive if args.archive is not None
                    else fetch_release_archive(args.target, work / "release.tar.gz"),
                    "release archive")
                official_sha = verify_pinned_archive(
                    archive, args.target,
                    registry["sha256"].get(args.target)
                    or registry["sha256"].get(f"{args.target}-baseline"))
                binary = binary_from_archive(archive, work)
                extracted = extract_bundle(binary, work)
            else:
                extracted = safe_absolute(args.extracted, "extraction directory")
            bundle_js = extracted / "freebuff.js"
            asset_dir = work / "assets-staged"
            stage_assets(args.target, extracted, asset_dir)
            build(args.target, bundle_js, asset_dir, args.output,
                  expected_bundle_sha256=registry["bundle_sha256"][args.target],
                  official_archive_sha256=official_sha)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Private CLI build failed: {error}\n")


if __name__ == "__main__":
    main()
