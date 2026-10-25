#!/usr/bin/env python3
"""Render the official CLI under a pty and fail when the TUI draws a blank frame.

Release sanity gate: the gateway runs the official executable unmodified, so
this checks the pinned release itself (and any release candidate before its
digests are pinned). It fails on damaged assets, renderer load errors, and
blank or text-less frames — the historical failure mode hit when CLI builds
were still being rebuilt.

The binary under test is always the fixed project-relative path
``.freebuff/render-smoke/freebuff``: copy the build there first. Nothing from
the command line ever influences what gets executed; the only argument is the
capture duration in seconds.

Usage: render_smoke_check.py [SECONDS]
Exit status 0 means a non-blank frame was observed.
"""

import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]

# The fixed subject of this smoke test, executed with the project root as the
# working directory. The subprocess argv below stays a literal constant.
CANDIDATE_RELATIVE = ".freebuff/render-smoke/freebuff"
CANDIDATE = PROJECT / ".freebuff" / "render-smoke" / "freebuff"
REQUIRED_BINARY_NAME = "freebuff"

DEFAULT_SECONDS = 20.0
MAX_SECONDS = 120.0
# A real terminal answers capability queries before the TUI paints text; a
# dumb pipe leaves every binary stuck on a borders-only frame, which is why
# the harness answers the common ones itself.
QUERY_REPLIES = (
    (b"\x1b[c", b"\x1b[?62;9c"),            # primary device attributes
    (b"\x1b[>c", b"\x1b[>0;277;0c"),        # secondary device attributes
    (b"\x1b[6n", b"\x1b[1;1R"),             # cursor position report
    (b"\x1b[?u", b"\x1b[?0u"),              # kitty keyboard protocol
    (b"\x1b[?2026$p", b"\x1b[?2026;2$y"),   # synchronized output support
    (b"\x1b]10;?", b"\x1b]10;rgb:ffff/ffff/ffff\x07"),
    (b"\x1b]11;?", b"\x1b]11;rgb:0000/0000/0000\x07"),
)

# A frame with rendered UI text carries recognisable words; borders and
# cursor moves never do. This is the discriminator between a live TUI and the
# blank-frame regression.
WORD_RE = re.compile(rb"[A-Za-z]{3,}")
MIN_WORD_CHARACTERS = 120
# Secondary bar: a rendered frame also paints far more than bare control data.
MIN_PRINTABLE_CHARS = 400
FAILURE_MARKERS = (
    b"opentui is not supported",
    b"not supported on the current platform",
    b"no such file or directory",
    # A fatal error dialog renders text too; it must still fail the check.
    b"fatal error during startup",
    b"could not load its terminal renderer",
    b"failed initialize opentui render library",
)

ANSI_ESCAPES = re.compile(
    rb"\x1b(?:\[[0-9;?<=>!]*[ -/]*[@-~]"     # CSI sequences
    rb"|\][^\x07\x1b]*(?:\x07|\x1b\\)"       # OSC sequences
    rb"|[@-Z\\-_])"                          # two-byte escapes
)
CONTROL_BYTES = re.compile(rb"[\x00-\x1f\x7f-\x9f]")


def check_candidate():
    """Refuse to run until the fixed candidate path holds a freebuff build."""
    if CANDIDATE.name != REQUIRED_BINARY_NAME or not CANDIDATE.is_file() \
            or not os.access(CANDIDATE, os.X_OK):
        raise SystemExit(
            f"Copy the CLI build to {CANDIDATE} first; it must be an "
            f"executable file named '{REQUIRED_BINARY_NAME}'.")


def parse_seconds(argument):
    """Accept only a plain positive number as the capture duration."""
    try:
        seconds = float(argument)
    except ValueError:
        raise SystemExit(f"SECONDS must be a number, not {argument!r}") from None
    if not 0 < seconds <= MAX_SECONDS:
        raise SystemExit(f"SECONDS must be between 0 and {MAX_SECONDS}")
    return seconds


def spawn(seconds):
    """Run the candidate binary on a fresh pty and return everything it drew."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    home = tempfile.mkdtemp(prefix="freebuff-render-smoke-")
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": home,
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
        "CODEBUFF_SHIP_LOGS": "false",
        "NO_COLOR": "1",
    }
    process = None
    captured = bytearray()
    try:
        process = subprocess.Popen(
            [".freebuff/render-smoke/freebuff"], cwd=str(PROJECT),
            stdin=slave, stdout=slave, stderr=slave,
            env=environment, close_fds=True)
        os.close(slave)
        slave = None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.5)
            if not ready:
                if process.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            captured.extend(chunk)
            replies = b"".join(reply for query, reply in QUERY_REPLIES if query in chunk)
            if replies:
                os.write(master, replies)
    finally:
        if slave is not None:
            os.close(slave)
        if process is not None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        os.close(master)
    return bytes(captured)


def printable_text(raw):
    cleaned = ANSI_ESCAPES.sub(b"", raw)
    cleaned = CONTROL_BYTES.sub(b"", cleaned)
    return cleaned


def main():
    check_candidate()
    seconds = parse_seconds(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SECONDS
    raw = spawn(seconds)
    text = printable_text(raw)
    printable = sum(1 for byte in text if byte != 0x20)
    found_words = WORD_RE.findall(text)
    words = b" ".join(found_words).decode("ascii", "replace")
    word_characters = sum(len(word) for word in found_words)

    failures = []
    for marker in FAILURE_MARKERS:
        if marker in raw.lower():
            failures.append(f"failure marker {marker!r} appeared in the output")
    if word_characters < MIN_WORD_CHARACTERS:
        failures.append(
            f"only {word_characters} word characters were drawn "
            f"(expected at least {MIN_WORD_CHARACTERS}); the TUI rendered no text")
    if printable < MIN_PRINTABLE_CHARS:
        failures.append(
            f"only {printable} non-space printable characters were drawn "
            f"(expected at least {MIN_PRINTABLE_CHARS}); the TUI rendered blank frames")

    sample = text[:2000].decode("utf-8", "replace")
    print(f"captured {len(raw):,} raw bytes, {printable:,} printable characters, "
          f"{word_characters:,} word characters")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        print("--- words drawn ---")
        print(words[:800] or "(none)")
        print("--- first captured frame text ---")
        print(sample[:800] or "(nothing)")
        return 1
    print("OK: the TUI drew a non-blank frame with text")
    print("--- words drawn ---")
    print(words[:800])
    return 0


if __name__ == "__main__":
    sys.exit(main())
