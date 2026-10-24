import os
import sys
import plistlib
import subprocess
import logging
from typing import Dict, Any, Optional, Tuple

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
    Constructs an authentic, complete terminal environment dictionary.
    Inherits standard host variables and injects proper interactive desktop terminal attributes.
    Supports both macOS and Linux cloud containers seamlessly.
    """
    env = os.environ.copy()

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

    # Locale and Encoding
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
    cur_path = env.get("PATH", "")
    safe_paths = []
    for d in cur_path.split(":"):
        if d and not d.startswith("/Users/") and not d.startswith("/home/"):
            if d not in safe_paths:
                safe_paths.append(d)
    for d in path_dirs:
        if d not in safe_paths:
            safe_paths.append(d)
    env["PATH"] = ":".join(safe_paths)

    # Host leakage scrubber: purge sensitive macOS and host-identifying environment variables
    sensitive_prefixes = ("Apple_", "XPC_", "SECURITY", "__CF", "LaunchInstanceID")
    sensitive_keys = {
        "ORIGINAL_XDG_CURRENT_DESKTOP", "MallocNanoZone", "COMMAND_MODE",
        "ITERM_SESSION_ID", "VSCODE_INJECTION", "TERM_SESSION_ID"
    }
    for k in list(env.keys()):
        if any(k.startswith(p) for p in sensitive_prefixes) or k in sensitive_keys:
            del env[k]

    # Do NOT inject CODEBUFF_API_KEY as it overrides the native OAuth keychain session
    # and causes Freebuff to fall back to invalid API key validation.

    if custom_overrides:
        env.update(custom_overrides)

    return env

def compute_simulated_x_freebuff_env(env: Dict[str, str], parent_kind: str = "shell", grandparent_kind: str = "terminal") -> str:
    """
    Simulates the exact `x-freebuff-env` descriptor string that Freebuff CLI's
    client-environment.ts produces for this environment.
    Format: v1;in=1;out=1;tp=<bucket>;term=1;ct=1;sz=120x40;ci=0;ssh=0;l=1;p=<parent>;g=<grandparent>;...
    """
    tp_raw = (env.get("TERM_PROGRAM") or "").lower()
    tp_bucket = "none"
    if "iterm" in tp_raw:
        tp_bucket = "iterm"
    elif "apple_terminal" in tp_raw or "terminal" in tp_raw:
        tp_bucket = "apple_terminal"
    elif "vscode" in tp_raw:
        tp_bucket = "vscode"
    elif "ghostty" in tp_raw:
        tp_bucket = "ghostty"
    elif tp_raw:
        tp_bucket = "other"

    term_flag = 1 if env.get("TERM") else 0
    ct_flag = 1 if env.get("COLORTERM") else 0
    sz = f"{env.get('COLUMNS', '120')}x{env.get('LINES', '40')}"
    ssh_flag = 1 if (env.get("SSH_CLIENT") or env.get("SSH_TTY")) else 0
    ci_flag = 1 if env.get("CI") else 0

    fields = [
        ("in", "1"),
        ("out", "1"),
        ("tp", tp_bucket),
        ("term", str(term_flag)),
        ("ct", str(ct_flag)),
        ("sz", sz),
        ("ci", str(ci_flag)),
        ("ssh", str(ssh_flag)),
        ("l", "1"),
        ("p", parent_kind),
        ("g", grandparent_kind),
        ("osc", "1"),
        ("tzo", "0"),
        ("px", "none"),
        ("tls", "1"),
        ("ca", "0")
    ]
    return "v1;" + ";".join(f"{k}={v}" for k, v in fields)

def audit_runtime_environment(pid: Optional[int] = None) -> Dict[str, Any]:
    """
    Audits the current or specified process environment and ancestry.
    Returns structured verification results.
    """
    target_pid = pid or os.getpid()
    
    ancestry = []
    curr = target_pid
    for _ in range(4):
        try:
            out = subprocess.check_output(
                ["ps", "-o", "pid=,ppid=,comm=", "-p", str(curr)],
                stderr=subprocess.DEVNULL
            ).decode().strip()
            if not out:
                break
            parts = out.split(None, 2)
            if len(parts) >= 3:
                p_pid, p_ppid, p_comm = int(parts[0]), int(parts[1]), parts[2]
                ancestry.append({"pid": p_pid, "ppid": p_ppid, "comm": p_comm})
                curr = p_ppid
                if p_ppid <= 1:
                    break
            else:
                break
        except Exception:
            break

    parent_kind = "unknown"
    grandparent_kind = "unknown"
    if len(ancestry) >= 2:
        p_name = ancestry[1]["comm"].lower()
        if "zsh" in p_name or "bash" in p_name or "sh" in p_name:
            parent_kind = "shell"
        elif "python" in p_name:
            parent_kind = "python"
        elif "launchd" in p_name:
            parent_kind = "launchd"
        else:
            parent_kind = "other"

    if len(ancestry) >= 3:
        gp_name = ancestry[2]["comm"].lower()
        if "zsh" in gp_name or "bash" in gp_name:
            grandparent_kind = "shell"
        elif "launchd" in gp_name:
            grandparent_kind = "launchd"
        elif "terminal" in gp_name or "iterm" in gp_name:
            grandparent_kind = "terminal"
        else:
            grandparent_kind = "other"

    env = get_standard_terminal_env()
    simulated_descriptor = compute_simulated_x_freebuff_env(env, parent_kind=parent_kind, grandparent_kind=grandparent_kind)

    return {
        "target_pid": target_pid,
        "ancestry": ancestry,
        "parent_kind": parent_kind,
        "grandparent_kind": grandparent_kind,
        "terminal_program": env.get("TERM_PROGRAM"),
        "terminal_program_version": env.get("TERM_PROGRAM_VERSION"),
        "shell": env.get("SHELL"),
        "lang": env.get("LANG"),
        "term": env.get("TERM"),
        "colorterm": env.get("COLORTERM"),
        "simulated_x_freebuff_env": simulated_descriptor,
        "is_p_python_eliminated": parent_kind != "python",
        "is_interactive_shell_parent": parent_kind == "shell",
    }
