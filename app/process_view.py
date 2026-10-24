"""Platform-specific supervisor names for the CLI's ancestry query.

macOS uses a scoped ps response; Linux sets this supervisor's own kernel comm.
Neither changes process ownership, ancestry, or the executable being run.
"""

import ctypes
import os
from pathlib import Path
import sys


SHIM_DIR = Path(__file__).resolve().parent / "fake_bin"
SHIM_SETTING = "FREEBUFF_SUPERVISOR_PS_SHIM"
SUPERVISOR_PID_KEY = "_FREEBUFF_PS_SUPERVISOR_PID"
SHIM_DIR_KEY = "_FREEBUFF_PS_SHIM_DIR"
LINUX_SUPERVISOR_NAME = b"terminal"


def supervisor_terminal_view_enabled(env=None, *, platform=None):
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    return platform in {"darwin", "linux"} and env.get(SHIM_SETTING, "true").strip().lower() in {"1", "true"}


def supervisor_ps_shim_enabled(env=None, *, platform=None):
    platform = sys.platform if platform is None else platform
    return platform == "darwin" and supervisor_terminal_view_enabled(env, platform=platform)


def configure_supervisor_process_name(env=None, *, platform=None):
    """Name only the calling Linux supervisor before it launches any children.

    PR_SET_NAME changes the calling task's comm (visible in /proc/PID/stat and
    ps), and requires no root privileges. The supervisor is single-threaded.
    It does not change argv, /proc/PID/exe, PID, PPID, or process-group ownership.
    """
    platform = sys.platform if platform is None else platform
    if platform != "linux" or not supervisor_terminal_view_enabled(env, platform=platform):
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(15, LINUX_SUPERVISOR_NAME, 0, 0, 0) != 0:  # PR_SET_NAME
        error = ctypes.get_errno()
        raise OSError(error, "Cannot set Linux supervisor process name: " + os.strerror(error))
    return True


def supervisor_child_environment(env=None, *, supervisor_pid=None, platform=None):
    """Bind the shim to this supervisor, never an inherited worker's PID."""
    child_env = dict(os.environ if env is None else env)
    previous_dir = child_env.pop(SHIM_DIR_KEY, None)
    child_env.pop(SUPERVISOR_PID_KEY, None)
    paths = [
        entry for entry in child_env.get("PATH", os.defpath).split(os.pathsep)
        if entry not in {str(SHIM_DIR), previous_dir}
    ]
    child_env["PATH"] = os.pathsep.join(paths)
    if not supervisor_ps_shim_enabled(child_env, platform=platform):
        return child_env

    shim = SHIM_DIR / "ps"
    if not shim.is_file() or not os.access(shim, os.X_OK):
        raise RuntimeError(f"Worker ps shim is missing or not executable: {shim}")
    pid = os.getpid() if supervisor_pid is None else supervisor_pid
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise ValueError("A live supervisor PID greater than one is required")
    child_env[SUPERVISOR_PID_KEY] = str(pid)
    child_env[SHIM_DIR_KEY] = str(SHIM_DIR)
    child_env["PATH"] = os.pathsep.join([str(SHIM_DIR), *paths])
    return child_env


# Login-shell startup files can reorder PATH. Apply the prefix after those
# files have run, immediately before the CLI; quote paths containing spaces.
RESTORE_SHIM_PATH = (
    'if [ -n "${_FREEBUFF_PS_SHIM_DIR:-}" ]; then '
    'PATH="$_FREEBUFF_PS_SHIM_DIR:$PATH"; export PATH; fi; '
)
