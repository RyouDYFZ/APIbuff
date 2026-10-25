import os
import sys
import plistlib
import subprocess
import logging
import math
import re
from typing import Dict, Any, Mapping, Optional, Tuple, Union
from urllib.parse import urlsplit

logger = logging.getLogger("freebuff.env")

# Terminal program canonical mappings matching Freebuff client descriptor
VALID_TERMINAL_PROGRAMS = {
    "apple_terminal": "Apple_Terminal",
    "iterm": "iTerm.app",
    "iterm2": "iTerm.app",
    "vscode": "vscode",
    "ghostty": "ghostty",
    "wezterm": "wezterm",
    "warp": "WarpTerminal",
    "hyper": "hyper",
    "tmux": "tmux",
    "alacritty": "alacritty",
    "kitty": "kitty",
}

def detect_terminal_program() -> Tuple[str, str]:
    """
    Detects the host system's terminal application and its version.
    Returns (terminal_program_name, version_string).
    """
    # 1. Check existing environment if already set
    env_prog = os.environ.get("TERM_PROGRAM")
    env_ver = os.environ.get("TERM_PROGRAM_VERSION")
    if env_prog and env_ver:
        return env_prog, env_ver

    # 2. Check for macOS Terminal.app (default system terminal)
    terminal_plist = "/System/Applications/Utilities/Terminal.app/Contents/Info.plist"
    if os.path.exists(terminal_plist):
        try:
            with open(terminal_plist, "rb") as f:
                plist = plistlib.load(f)
                version = plist.get("CFBundleShortVersionString", "2.15")
                return "Apple_Terminal", str(version)
        except Exception as e:
            logger.debug(f"Failed reading Terminal.app plist: {e}")

    # 3. Check for iTerm2
    iterm_plist = "/Applications/iTerm.app/Contents/Info.plist"
    if os.path.exists(iterm_plist):
        try:
            with open(iterm_plist, "rb") as f:
                plist = plistlib.load(f)
                version = plist.get("CFBundleShortVersionString", "3.5.11")
                return "iTerm.app", str(version)
        except Exception:
            pass

    # 4. Check Ghostty
    ghostty_plist = "/Applications/Ghostty.app/Contents/Info.plist"
    if os.path.exists(ghostty_plist):
        try:
            with open(ghostty_plist, "rb") as f:
                plist = plistlib.load(f)
                version = plist.get("CFBundleShortVersionString", "1.0.0")
                return "ghostty", str(version)
        except Exception:
            pass

    # 5. Linux / Cloud Container fallback: authentic VS Code / iTerm terminal
    if sys.platform != "darwin":
        return "vscode", "1.95.0"

    return "Apple_Terminal", "2.15"

def get_standard_terminal_env(
    custom_overrides: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    """
    Construct a minimal, complete terminal environment dictionary.

    Nothing is inherited from the gateway host except an explicit allowlist:
    gateway secrets (FREEBUFF_API_KEY), cloud credentials, host identity, and
    geolocation hints (TZ) must never reach the third-party CLI process.
    """
    env: Dict[str, str] = {}

    term_prog, term_ver = detect_terminal_program()

    # Core Terminal Environment
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["TERM_PROGRAM"] = term_prog
    env["TERM_PROGRAM_VERSION"] = term_ver

    # Shell and User Context (autodetect zsh, bash, or sh on Linux)
    import shutil
    shell_candidate = os.environ.get("SHELL") or shutil.which("zsh") or shutil.which("bash") or "/bin/sh"
    env["SHELL"] = shell_candidate
    env["HOME"] = os.path.expanduser("~")
    # Neutralize host user identity to prevent telemetry de-anonymization
    env["USER"] = "developer"
    env["LOGNAME"] = "developer"

    # Locale and Encoding: inherited only when the host set them (content
    # language); they are not part of the upstream environment descriptor.
    env["LANG"] = os.environ.get("LANG", "en_US.UTF-8")
    env["LC_ALL"] = os.environ.get("LC_ALL", "en_US.UTF-8")
    env["LC_CTYPE"] = os.environ.get("LC_CTYPE", "en_US.UTF-8")

    # Geometry defaults for Freebuff TUI
    env["COLUMNS"] = "120"
    env["LINES"] = "40"

    # Clean PATH ensuring standard mac binaries and Homebrew are reachable,
    # while actively scrubbing any personal host user directories
    path_dirs = [
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    cur_path = os.environ.get("PATH", "")
    safe_paths = []
    for d in cur_path.split(":"):
        if d and not d.startswith("/Users/") and not d.startswith("/home/") and not d.startswith("~"):
            if d not in safe_paths:
                safe_paths.append(d)
    for d in path_dirs:
        if d not in safe_paths:
            safe_paths.append(d)
    env["PATH"] = ":".join(safe_paths)

    # Do NOT inject CODEBUFF_API_KEY as it overrides the native OAuth keychain session
    # and causes Freebuff to fall back to invalid API key validation.

    if custom_overrides:
        env.update(custom_overrides)

    # Repository code trust is controlled only by the gateway's explicit
    # FREEBUFF_TRUST_AGENTS opt-in. A host-wide CLI opt-in must not bypass it.
    env.pop("CODEBUFF_TRUST_AGENT_DIRS", None)

    # Separate controls: /api/logs is upstream-supported and stays off; the
    # PostHog SDK ignores every environment switch, so its egress is refused
    # by the gateway proxy instead (app/proxy_manager).
    env["CODEBUFF_SHIP_LOGS"] = "false"
    # The gateway owns and closes this PTY; no detached terminal-reset helper is needed.
    env["CODEBUFF_NO_TERMINAL_WATCHDOG"] = "1"

    return env

_SHELL_NAMES = frozenset({
    "sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "tcsh", "csh", "nu",
    "elvish", "xonsh", "pwsh", "powershell", "cmd", "login",
})
_TERMINAL_NAMES = frozenset({
    "terminal", "iterm2", "ghostty", "wezterm", "wezterm-gui", "alacritty",
    "kitty", "warp", "stable", "hyper", "tabby", "konsole", "gnome-terminal",
    "gnome-terminal-server", "gnome-terminal-", "xfce4-terminal", "mate-terminal",
    "lxterminal", "tilix", "terminator", "xterm", "urxvt", "rxvt", "st", "foot",
    "rio", "mintty", "windowsterminal", "openconsole", "conhost", "contour",
    "blackbox", "ptyxis", "ptyxis-agent",
})
_EDITOR_NAMES = frozenset({
    "code", "code-insiders", "cursor", "windsurf", "zed", "idea", "pycharm",
    "webstorm", "goland", "clion", "rider", "phpstorm", "rubymine", "datagrip",
    "fleet", "nvim", "vim", "emacs", "helix", "hx", "sublime_text",
})
_MULTIPLEXER_NAMES = frozenset({"tmux", "screen", "zellij", "byobu", "abduco"})
_PROCESS_KINDS = frozenset({
    "shell", "terminal", "editor", "mux", "sshd", "node", "python", "bun",
    "other", "unknown", "na",
})
_TERMINAL_PROGRAM_BUCKETS = {
    "apple_terminal": "apple_terminal", "iterm.app": "iterm", "iterm2": "iterm",
    "vscode": "vscode", "ghostty": "ghostty", "wezterm": "wezterm",
    "warpterminal": "warp", "hyper": "hyper", "tmux": "tmux", "zed": "zed",
    "tabby": "tabby", "rio": "rio", "mintty": "mintty",
    "jetbrains-jediterm": "jetbrains", "kitty": "kitty", "alacritty": "alacritty",
}


def bucket_process_name(raw_name: Optional[str]) -> str:
    """Apply the extracted CLI's fixed process categories to a ps comm value."""
    if not raw_name:
        return "unknown"
    name = raw_name.strip().rsplit("/", 1)[-1].lstrip("-")
    name = re.sub(r"\.exe$", "", name, flags=re.IGNORECASE).lower()
    if not name:
        return "unknown"
    if name in _SHELL_NAMES:
        return "shell"
    if name in _MULTIPLEXER_NAMES or name.startswith("tmux"):
        return "mux"
    if name == "sshd" or name.startswith("sshd-"):
        return "sshd"
    if name in ("node", "nodejs"):
        return "node"
    if re.fullmatch(r"python[0-9.]*", name) or name in ("pypy", "pypy3"):
        return "python"
    if name == "bun":
        return "bun"
    if name in _TERMINAL_NAMES:
        return "terminal"
    if name in _EDITOR_NAMES or re.match(r"^(code|cursor|windsurf|zed|electron)( helper|$)", name):
        return "editor"
    return "other"


def _proxy_bucket(value: Optional[str]) -> str:
    raw = (value or "").strip()
    if not raw:
        return "none"
    try:
        host = urlsplit(raw if "://" in raw else "http://" + raw).hostname or ""
        if re.fullmatch(r"localhost|127(?:\.\d{1,3}){3}|::1|0\.0\.0\.0", host, re.IGNORECASE):
            return "loopback"
    except ValueError:
        pass
    return "remote"


def _configured_dimension(value: Optional[str]) -> int:
    try:
        number = float(value)
        if math.isfinite(number):
            return max(0, min(9999, math.floor(number)))
    except (TypeError, ValueError, OverflowError):
        pass
    return 0


def compute_simulated_x_freebuff_env(
    env: Mapping[str, str],
    parent_kind: str = "unknown",
    grandparent_kind: str = "unknown",
    *,
    launcher: bool = False,
    system_zone: Optional[str] = None,
) -> str:
    """Predict a descriptor from configuration; this is not a captured header.

    Process categories follow the extracted CLI. TTY flags assume the worker's
    PTY and dimensions come from configuration, not target-process observations.
    The terminal's colour reply is unknown. A system zone must be supplied
    separately because the TZ override does not identify the OS time zone.
    """
    tp_raw = (env.get("TERM_PROGRAM") or "").strip().lower()
    tp_bucket = _TERMINAL_PROGRAM_BUCKETS.get(tp_raw, "other") if tp_raw else "none"
    proxy_buckets = [_proxy_bucket(env.get(key)) for key in (
        "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
    )]
    proxy = "loopback" if "loopback" in proxy_buckets else "remote" if "remote" in proxy_buckets else "none"
    cloud_ide = "none"
    if env.get("CODESPACES", "").strip().lower() == "true":
        cloud_ide = "codespaces"
    elif env.get("GITPOD_WORKSPACE_ID", "").strip():
        cloud_ide = "gitpod"
    elif env.get("CLOUD_SHELL", "").strip().lower() == "true":
        cloud_ide = "cloudshell"
    elif env.get("CODER", "").strip().lower() == "true":
        cloud_ide = "coder"
    tz_override = bool(env.get("TZ", "").strip())
    fields = [
        ("in", "1"),
        ("out", "1"),
        ("tp", tp_bucket),
        ("term", str(int(bool(env.get("TERM"))))),
        ("ct", str(int(bool(env.get("COLORTERM"))))),
        ("sz", f"{_configured_dimension(env.get('COLUMNS'))}x{_configured_dimension(env.get('LINES'))}"),
        ("ci", str(int(env.get("CI") in ("true", "1") or env.get("GITHUB_ACTIONS") == "true"))),
        ("ssh", str(int(bool(env.get("SSH_TTY") or env.get("SSH_CONNECTION"))))),
        ("l", str(int(bool(launcher)))),
        ("p", parent_kind if parent_kind in _PROCESS_KINDS else "unknown"),
        ("g", grandparent_kind if grandparent_kind in _PROCESS_KINDS else "unknown"),
        ("osc", "na"),
        ("tzo", str(int(tz_override))),
        ("px", proxy),
        ("tls", "0" if env.get("NODE_TLS_REJECT_UNAUTHORIZED", "").strip() == "0" else "1"),
        ("ca", str(int(bool(env.get("NODE_EXTRA_CA_CERTS", "").strip())))),
        ("cde", cloud_ide),
    ]
    if tz_override and system_zone:
        zone = system_zone.strip().rsplit("zoneinfo/", 1)[-1]
        if len(zone) <= 64 and re.fullmatch(r"[A-Za-z]+(?:/[A-Za-z0-9_+-]+){1,2}", zone):
            fields.append(("stz", zone))
    return "v1;" + ";".join(f"{key}={value}" for key, value in fields)


def audit_runtime_environment(
    pid: Optional[int] = None,
    *,
    launcher_pid: Optional[Union[int, str]] = None,
    descriptor_env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Read a process ancestry snapshot and predict its configured descriptor.

    The caller must identify an npm launcher explicitly. An inherited launcher
    variable in gateway configuration is not evidence about the target. No
    target-process environment or request headers are read by this diagnostic.
    """
    target_pid = os.getpid() if pid is None else pid
    ancestry = []
    curr = target_pid
    seen = set()
    # The diagnostic observes the real process tree even when a worker's PATH
    # contains a compatibility shim for CLI process-name queries.
    ps_path = "/bin/ps" if sys.platform == "darwin" else "/usr/bin/ps"
    if not os.path.isfile(ps_path):
        ps_path = "/bin/ps"
    # The target plus two ancestors, and one extra for a confirmed npm launcher.
    for _ in range(0 if sys.platform == "win32" else 4):
        if not isinstance(curr, int) or isinstance(curr, bool) or curr < 1 or curr in seen:
            break
        seen.add(curr)
        try:
            out = subprocess.check_output(
                [ps_path, "-o", "pid=,ppid=,comm=", "-p", str(curr)],
                timeout=2, stderr=subprocess.DEVNULL,
            ).decode().strip()
            parts = out.splitlines()[0].split(None, 2) if out else []
            if len(parts) != 3:
                break
            process_pid, parent_pid, command = int(parts[0]), int(parts[1]), parts[2]
            if process_pid != curr:
                break
            entry = {"pid": process_pid, "ppid": parent_pid, "comm": command}
            if sys.platform == "linux":
                # Linux comm is configurable by the process itself. Preserve
                # executable evidence separately instead of treating the name
                # as proof that a terminal application is actually running.
                try:
                    entry["executable"] = os.readlink(f"/proc/{process_pid}/exe")
                except OSError:
                    pass
            ancestry.append(entry)
            curr = parent_pid
        except (OSError, ValueError, subprocess.SubprocessError):
            break

    launcher = bool(ancestry and launcher_pid is not None and str(launcher_pid) == str(ancestry[0]["ppid"]))
    parent_index = 2 if launcher else 1
    parent_kind = bucket_process_name(ancestry[parent_index]["comm"] if len(ancestry) > parent_index else None)
    grandparent_kind = bucket_process_name(ancestry[parent_index + 1]["comm"] if len(ancestry) > parent_index + 1 else None)
    if sys.platform == "win32":
        parent_kind = grandparent_kind = "na"
        launcher = False

    env = get_standard_terminal_env() if descriptor_env is None else descriptor_env
    simulated_descriptor = compute_simulated_x_freebuff_env(
        env, parent_kind=parent_kind, grandparent_kind=grandparent_kind, launcher=launcher,
    )
    return {
        "target_pid": target_pid,
        "ancestry": ancestry,
        "ancestry_source": "system_process_snapshot",
        "parent_kind": parent_kind,
        "grandparent_kind": grandparent_kind,
        "launcher_skipped": launcher,
        "terminal_program": env.get("TERM_PROGRAM"),
        "terminal_program_version": env.get("TERM_PROGRAM_VERSION"),
        "shell": env.get("SHELL"),
        "lang": env.get("LANG"),
        "term": env.get("TERM"),
        "colorterm": env.get("COLORTERM"),
        "simulated_x_freebuff_env": simulated_descriptor,
        "descriptor_source": "prediction_from_gateway_configuration",
        "descriptor_environment_source": "gateway_configuration" if descriptor_env is None else "caller_configuration",
        "descriptor_verified": False,
        "predicted_fields": ["in", "out", "sz"],
        "unobserved_fields": ["osc"],
        "shell_parent_observed": parent_kind == "shell",
        "interactive_shell_verified": False,
    }
