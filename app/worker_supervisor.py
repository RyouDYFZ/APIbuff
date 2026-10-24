"""Keep a PTY process group alive only while its gateway owns the control pipe.

Run as a separate, single-threaded process so gateway SIGKILL is detected as EOF.
The supervisor owns/reaps its child and never searches for other CLI processes.
"""

import fcntl
import os
import selectors
import signal
import subprocess
import sys
import termios
import time

if __package__:
    from .process_view import configure_supervisor_process_name, supervisor_child_environment
    from .lifecycle import ensure_file_descriptor_limit
else:
    from process_view import configure_supervisor_process_name, supervisor_child_environment
    from lifecycle import ensure_file_descriptor_limit


STOP_GRACE_SECONDS = 2.0


def _controlling_terminal():
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin reports EPERM for a group containing only an unreaped zombie.
        # WNOWAIT checks the owned child without releasing its PID for reuse.
        if sys.platform == "darwin" and _child_exited(pgid):
            return False
        raise


def _child_exited(pid):
    return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def supervise(control_fd, terminal_fd, command):
    ensure_file_descriptor_limit(4096)
    configure_supervisor_process_name()
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGCHLD):
        signal.signal(sig, request_stop)

    child = subprocess.Popen(
        command,
        stdin=terminal_fd,
        stdout=terminal_fd,
        stderr=terminal_fd,
        start_new_session=True,
        preexec_fn=_controlling_terminal,
        close_fds=True,
        env=supervisor_child_environment(),
    )
    os.close(terminal_fd)
    # Only the gateway owns the write end; neither this process nor the CLI
    # inherits it. A gateway crash therefore closes this lease automatically.
    try:
        # Even if the gateway dies before consuming this handshake, the
        # resulting BrokenPipeError must still run process-group cleanup.
        print(child.pid, flush=True)
        with selectors.DefaultSelector() as selector:
            selector.register(control_fd, selectors.EVENT_READ)
            while not stopping and not _child_exited(child.pid):
                if selector.select(timeout=0.1):
                    if not os.read(control_fd, 1024):
                        break
    finally:
        os.close(control_fd)
        # SIGCHLD requests shutdown without reaping the group leader. Its PID
        # cannot be recycled until the final wait, even after an early exit.
        try:
            _signal_group(child.pid, signal.SIGTERM)
            deadline = time.monotonic() + STOP_GRACE_SECONDS
            while time.monotonic() < deadline:
                if not _signal_group(child.pid, 0):
                    break
                time.sleep(0.05)
            _signal_group(child.pid, signal.SIGKILL)
        finally:
            child.wait()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(supervise(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3:]))
    except Exception as exc:
        print(f"Worker supervisor failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
