"""Compile a prepared private-CLI working directory with Bun.

This module is intentionally free of operator input: it receives only a
private temporary working directory containing ``freebuff.js`` (the patched
bundle) and the staged pinned assets. Every subprocess argv below is a single
inline constant per target — executable name, fixed flags, and the pinned
asset names — so no caller-controlled or computed value reaches the compiler.
"""

from pathlib import Path
import os
import shutil
import subprocess

# Pinned embedded-asset names per build target. Each list must equal the
# sorted keys of the matching cli/assets-0.2.34-*.json registry; the build
# script verifies that before invoking this module.
DARWIN_ARM64_ASSETS = [
    "highlights-eq9cgrbb.scm", "highlights-ghv9g403.scm", "highlights-hk7bwhj4.scm",
    "highlights-r812a2qc.scm", "highlights-x6tmsnaa.scm", "injections-73j83es3.scm",
    "libopentui-c8ye7mqw.dylib", "rg-87es5scq.",
    "tree-sitter-c_sharp-tags-krggt7ep.scm", "tree-sitter-cpp-tags-8wb1fc43.scm",
    "tree-sitter-go-tags-051vehfr.scm", "tree-sitter-java-tags-hb8g75z5.scm",
    "tree-sitter-javascript-nd0q4pe9.wasm", "tree-sitter-javascript-tags-1vn24jqh.scm",
    "tree-sitter-markdown-411r6y9b.wasm", "tree-sitter-markdown_inline-j5349f42.wasm",
    "tree-sitter-python-tags-mtzz1sj5.scm", "tree-sitter-ruby-tags-bgd22x7y.scm",
    "tree-sitter-rust-tags-1pqgxchh.scm", "tree-sitter-typescript-tags-7thccg3r.scm",
    "tree-sitter-typescript-zxjzwt75.wasm", "tree-sitter-zig-e78zbjpm.wasm",
]

LINUX_X64_ASSETS = [
    "highlights-eq9cgrbb.scm", "highlights-ghv9g403.scm", "highlights-hk7bwhj4.scm",
    "highlights-r812a2qc.scm", "highlights-x6tmsnaa.scm", "injections-73j83es3.scm",
    "libopentui-c48jzvfh.so", "libopentui-xtk56e94.so", "rg-1nt2c2vg.",
    "tree-sitter-c_sharp-tags-krggt7ep.scm", "tree-sitter-cpp-tags-8wb1fc43.scm",
    "tree-sitter-go-tags-051vehfr.scm", "tree-sitter-java-tags-hb8g75z5.scm",
    "tree-sitter-javascript-nd0q4pe9.wasm", "tree-sitter-javascript-tags-1vn24jqh.scm",
    "tree-sitter-markdown-411r6y9b.wasm", "tree-sitter-markdown_inline-j5349f42.wasm",
    "tree-sitter-python-tags-mtzz1sj5.scm", "tree-sitter-ruby-tags-bgd22x7y.scm",
    "tree-sitter-rust-tags-1pqgxchh.scm", "tree-sitter-typescript-tags-7thccg3r.scm",
    "tree-sitter-typescript-zxjzwt75.wasm", "tree-sitter-zig-e78zbjpm.wasm",
]

LINUX_ARM64_ASSETS = [
    "highlights-eq9cgrbb.scm", "highlights-ghv9g403.scm", "highlights-hk7bwhj4.scm",
    "highlights-r812a2qc.scm", "highlights-x6tmsnaa.scm", "injections-73j83es3.scm",
    "libopentui-0r5w9btr.so", "libopentui-smaejjb8.so", "rg-bkfd568m.",
    "tree-sitter-c_sharp-tags-krggt7ep.scm", "tree-sitter-cpp-tags-8wb1fc43.scm",
    "tree-sitter-go-tags-051vehfr.scm", "tree-sitter-java-tags-hb8g75z5.scm",
    "tree-sitter-javascript-nd0q4pe9.wasm", "tree-sitter-javascript-tags-1vn24jqh.scm",
    "tree-sitter-markdown-411r6y9b.wasm", "tree-sitter-markdown_inline-j5349f42.wasm",
    "tree-sitter-python-tags-mtzz1sj5.scm", "tree-sitter-ruby-tags-bgd22x7y.scm",
    "tree-sitter-rust-tags-1pqgxchh.scm", "tree-sitter-typescript-tags-7thccg3r.scm",
    "tree-sitter-typescript-zxjzwt75.wasm", "tree-sitter-zig-e78zbjpm.wasm",
]

TARGET_ASSETS = {
    "darwin-arm64": DARWIN_ARM64_ASSETS,
    "linux-x64": LINUX_X64_ASSETS,
    "linux-arm64": LINUX_ARM64_ASSETS,
}


def compile_private_cli(target: str, work: Path, timeout: int = 1200) -> Path:
    """Run the Bun compiler inside the prepared working directory.

    ``work`` must already contain ``freebuff.js`` and every staged asset named
    in the target's pinned list. The subprocess environment is rebuilt from
    constants plus the temporary directory: no inherited credentials, build
    flags, NODE_OPTIONS, or BUN_OPTIONS.
    """
    if target not in TARGET_ASSETS:
        raise ValueError(f"Unsupported build target: {target}")
    missing = [name for name in TARGET_ASSETS[target] if not (work / name).is_file()]
    if missing:
        raise ValueError(f"Staged assets are missing from the build directory: {missing[0]}")
    if not (work / "freebuff.js").is_file():
        raise ValueError("The patched bundle entrypoint is missing from the build directory")
    bun = shutil.which("bun")
    if not bun:
        raise ValueError("Bun is required on PATH to compile the private CLI")
    (work / "tmp").mkdir(exist_ok=True)
    # The argv stays a constant; bun's directory is resolved through PATH.
    path = os.pathsep.join([os.path.dirname(bun), os.defpath])
    env = {"PATH": path, "HOME": str(work), "TMPDIR": str(work / "tmp")}
    if target == "darwin-arm64":
        subprocess.run(["bun", "build", "--compile", "--env=disable", "--external", "ws",
                        "--no-compile-autoload-dotenv", "--no-compile-autoload-bunfig",
                        "--no-compile-autoload-tsconfig", "--no-compile-autoload-package-json",
                        "--outfile", "freebuff",
                        "--asset", DARWIN_ARM64_ASSETS[0],
                        "--asset", DARWIN_ARM64_ASSETS[1],
                        "--asset", DARWIN_ARM64_ASSETS[2],
                        "--asset", DARWIN_ARM64_ASSETS[3],
                        "--asset", DARWIN_ARM64_ASSETS[4],
                        "--asset", DARWIN_ARM64_ASSETS[5],
                        "--asset", DARWIN_ARM64_ASSETS[6],
                        "--asset", DARWIN_ARM64_ASSETS[7],
                        "--asset", DARWIN_ARM64_ASSETS[8],
                        "--asset", DARWIN_ARM64_ASSETS[9],
                        "--asset", DARWIN_ARM64_ASSETS[10],
                        "--asset", DARWIN_ARM64_ASSETS[11],
                        "--asset", DARWIN_ARM64_ASSETS[12],
                        "--asset", DARWIN_ARM64_ASSETS[13],
                        "--asset", DARWIN_ARM64_ASSETS[14],
                        "--asset", DARWIN_ARM64_ASSETS[15],
                        "--asset", DARWIN_ARM64_ASSETS[16],
                        "--asset", DARWIN_ARM64_ASSETS[17],
                        "--asset", DARWIN_ARM64_ASSETS[18],
                        "--asset", DARWIN_ARM64_ASSETS[19],
                        "--asset", DARWIN_ARM64_ASSETS[20],
                        "--asset", DARWIN_ARM64_ASSETS[21],
                        "freebuff.js"],
                       shell=False, check=True, cwd=work, env=env, timeout=timeout)
    elif target == "linux-x64":
        subprocess.run(["bun", "build", "--compile", "--env=disable", "--external", "ws",
                        "--no-compile-autoload-dotenv", "--no-compile-autoload-bunfig",
                        "--no-compile-autoload-tsconfig", "--no-compile-autoload-package-json",
                        "--outfile", "freebuff",
                        "--asset", LINUX_X64_ASSETS[0],
                        "--asset", LINUX_X64_ASSETS[1],
                        "--asset", LINUX_X64_ASSETS[2],
                        "--asset", LINUX_X64_ASSETS[3],
                        "--asset", LINUX_X64_ASSETS[4],
                        "--asset", LINUX_X64_ASSETS[5],
                        "--asset", LINUX_X64_ASSETS[6],
                        "--asset", LINUX_X64_ASSETS[7],
                        "--asset", LINUX_X64_ASSETS[8],
                        "--asset", LINUX_X64_ASSETS[9],
                        "--asset", LINUX_X64_ASSETS[10],
                        "--asset", LINUX_X64_ASSETS[11],
                        "--asset", LINUX_X64_ASSETS[12],
                        "--asset", LINUX_X64_ASSETS[13],
                        "--asset", LINUX_X64_ASSETS[14],
                        "--asset", LINUX_X64_ASSETS[15],
                        "--asset", LINUX_X64_ASSETS[16],
                        "--asset", LINUX_X64_ASSETS[17],
                        "--asset", LINUX_X64_ASSETS[18],
                        "--asset", LINUX_X64_ASSETS[19],
                        "--asset", LINUX_X64_ASSETS[20],
                        "--asset", LINUX_X64_ASSETS[21],
                        "--asset", LINUX_X64_ASSETS[22],
                        "freebuff.js"],
                       shell=False, check=True, cwd=work, env=env, timeout=timeout)
    else:
        subprocess.run(["bun", "build", "--compile", "--env=disable", "--external", "ws",
                        "--no-compile-autoload-dotenv", "--no-compile-autoload-bunfig",
                        "--no-compile-autoload-tsconfig", "--no-compile-autoload-package-json",
                        "--outfile", "freebuff",
                        "--asset", LINUX_ARM64_ASSETS[0],
                        "--asset", LINUX_ARM64_ASSETS[1],
                        "--asset", LINUX_ARM64_ASSETS[2],
                        "--asset", LINUX_ARM64_ASSETS[3],
                        "--asset", LINUX_ARM64_ASSETS[4],
                        "--asset", LINUX_ARM64_ASSETS[5],
                        "--asset", LINUX_ARM64_ASSETS[6],
                        "--asset", LINUX_ARM64_ASSETS[7],
                        "--asset", LINUX_ARM64_ASSETS[8],
                        "--asset", LINUX_ARM64_ASSETS[9],
                        "--asset", LINUX_ARM64_ASSETS[10],
                        "--asset", LINUX_ARM64_ASSETS[11],
                        "--asset", LINUX_ARM64_ASSETS[12],
                        "--asset", LINUX_ARM64_ASSETS[13],
                        "--asset", LINUX_ARM64_ASSETS[14],
                        "--asset", LINUX_ARM64_ASSETS[15],
                        "--asset", LINUX_ARM64_ASSETS[16],
                        "--asset", LINUX_ARM64_ASSETS[17],
                        "--asset", LINUX_ARM64_ASSETS[18],
                        "--asset", LINUX_ARM64_ASSETS[19],
                        "--asset", LINUX_ARM64_ASSETS[20],
                        "--asset", LINUX_ARM64_ASSETS[21],
                        "--asset", LINUX_ARM64_ASSETS[22],
                        "freebuff.js"],
                       shell=False, check=True, cwd=work, env=env, timeout=timeout)
    binary = work / "freebuff"
    if not binary.is_file():
        raise ValueError("The compiler did not produce the private CLI binary")
    return binary
