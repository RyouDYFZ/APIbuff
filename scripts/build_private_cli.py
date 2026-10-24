#!/usr/bin/env python3
"""Rebuild the audited 0.2.34 macOS arm64 bundle with a runtime PostHog opt-out.

Never executes the CLI, overwrites the installed official binary, or downloads
dependencies. Unknown bundles/targets are refused until separately audited.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.cli_build import AUDITED_BUNDLE_SHA256, PRIVATE_CLI, PRIVACY_BUILD_VERSION, sha256_file

BUNDLE_SHA256 = AUDITED_BUNDLE_SHA256
ORIGINAL_FACTORY = "function ud(T,A){return new gJT(T,{...A,before_send:LvT()})}"
PATCHED_FACTORY = (
    "function ud(T,A){if(gatewayPostHogDisabled())return gatewayDisabledPostHogClient();"
    "return new gJT(T,{...A,before_send:LvT()})}"
)


def patch_bundle(bundle: bytes, guard: str) -> str:
    if hashlib.sha256(bundle).hexdigest() != BUNDLE_SHA256:
        raise ValueError("Unsupported CLI bundle: audit its factory and target before rebuilding")
    source = bundle.decode("utf-8")
    if source.count(ORIGINAL_FACTORY) != 1 or source.count("new gJT(") != 1:
        raise ValueError("PostHog constructor coverage has changed")
    return guard + "\n" + source.replace(ORIGINAL_FACTORY, PATCHED_FACTORY)


def build(extracted: Path, output: Path, bun: str) -> None:
    if (platform.system(), platform.machine()) != ("Darwin", "arm64"):
        raise ValueError("This audited bundle and its native assets support macOS arm64 only")
    bundle = (extracted / "freebuff.js").read_bytes()
    guard_path = PROJECT / "cli" / "posthog-opt-out.js"
    guard = guard_path.read_bytes()
    source = patch_bundle(bundle, guard.decode("utf-8"))
    manifest = json.loads((extracted / "assets" / "assets.json").read_text())
    expected_assets = json.loads((PROJECT / "cli" / "assets-0.2.34-darwin-arm64.json").read_text())
    if manifest["asset_count"] != 22 or len(manifest["assets"]) != 22:
        raise ValueError("Expected the complete set of 22 embedded CLI assets")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.with_name(output.name + ".privacy.json").exists():
        raise ValueError("Output already exists; choose a new output path to preserve that build")

    with tempfile.TemporaryDirectory(prefix="freebuff-private-build-") as temporary:
        work = Path(temporary)
        (work / "freebuff.js").write_text(source)
        assets = []
        for item in manifest["assets"]:
            name = PurePosixPath(item["name"])
            if str(name.parent) != "/$bunfs/root" or not name.name:
                raise ValueError("Unexpected embedded asset path")
            original = (extracted / item["file"]).resolve()
            if not original.is_relative_to((extracted / "assets").resolve()):
                raise ValueError("Asset escapes extraction directory")
            data = original.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if (len(data) != item["bytes"] or (work / name.name).exists()
                    or expected_assets.get(name.name) != digest):
                raise ValueError("Invalid or duplicate embedded asset")
            (work / name.name).write_bytes(data)
            assets.append({"name": name.name, "sha256": digest})
        command = [
            bun, "build", "--compile", "--env=disable", "--external=ws",
            "--no-compile-autoload-dotenv", "--no-compile-autoload-bunfig",
            "--no-compile-autoload-tsconfig", "--no-compile-autoload-package-json",
            "--outfile", str(work / "freebuff"),
        ]
        for asset in assets:
            command.extend(["--asset", asset["name"]])
        command.append("freebuff.js")
        # No inherited build flags, credentials, NODE_OPTIONS, or BUN_OPTIONS.
        (work / "tmp").mkdir()
        build_env = {"PATH": os.defpath, "HOME": str(work), "TMPDIR": str(work / "tmp")}
        subprocess.run(command, cwd=work, env=build_env, check=True, timeout=120)
        binary = work / "freebuff"
        metadata = {
            "privacy_build_version": PRIVACY_BUILD_VERSION,
            "cli_version": "0.2.34",
            "platform": "darwin",
            "machine": "arm64",
            "posthog_switch": "CODEBUFF_DISABLE_POSTHOG",
            "source_bundle_sha256": BUNDLE_SHA256,
            "guard_sha256": hashlib.sha256(guard).hexdigest(),
            "binary_sha256": sha256_file(binary),
            "bun_version": subprocess.check_output([bun, "--version"], env=build_env, text=True).strip(),
            "assets": assets,
        }
        # Stage on the destination filesystem before installing the new build.
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
    print(f"Built private CLI: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extracted", type=Path, default=PROJECT / "extracted_sources")
    parser.add_argument("--output", type=Path, default=PRIVATE_CLI)
    parser.add_argument("--bun", default=shutil.which("bun"))
    args = parser.parse_args()
    if not args.bun:
        parser.error("Bun is required")
    try:
        build(args.extracted.resolve(), args.output.resolve(), str(Path(args.bun).resolve()))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Private CLI build failed: {error}\n")


if __name__ == "__main__":
    main()
