import os
import pty
import termios
import fcntl
import struct
import hashlib
import random
import time
import asyncio
import logging
import json
import glob
import re
import sys
import subprocess
import shlex
from pathlib import Path
from typing import Optional, Callable, Dict, Any, List

from .config import (
    FREEBUFF_BIN,
    WORKER_WORKDIR,
    TURN_TIMEOUT_SECONDS,
    SESSION_BOOT_TIMEOUT_SECONDS,
    TAKEOVER_AUTO_CONFIRM,
    TAKEOVER_COOLDOWN_SECONDS,
    ADMISSION_AUTO_CONFIRM,
    MAX_HOURLY_ADMISSION,
    WORKER_SPAWN_MODE,
    USE_LOGIN_SHELL,
    SHELL_PATH,
    JITTER_MODE,
    JITTER_MIN_MS,
    JITTER_MAX_MS,
    JITTER_DISTRIBUTION,
    JITTER_TARGET_MAX_CPS,
    PRE_ENTER_MIN_MS,
    PRE_ENTER_MAX_MS
)
from .cleaner import strip_ansi
from .terminal_env import get_standard_terminal_env, audit_runtime_environment
from .cli_build import verify_official_cli
from .process_view import RESTORE_SHIM_PATH
from .git_privacy import prepare_git_privacy_bin
from .hardware_privacy import build_worker_command
from .lifecycle import ensure_file_descriptor_limit
from .jitter import KeystrokeJitterSimulator, JitterConfig, JitterMetrics, write_all
from .credentials import account_config_dir, write_cli_credentials
from .response_reader import TranscriptReader, current_response, read_log_records, check_log_error
from .exceptions import (
    FreebuffException,
    FreebuffAuthError,
    FreebuffRateLimitError,
    FreebuffPermissionError,
    FreebuffServerError,
    FreebuffTimeoutError,
    FreebuffBadRequestError,
    FreebuffPaymentError,
    FreebuffInteractionRequiredError
)

logger = logging.getLogger("freebuff.worker")

# Common real-world terminal geometries. Each account draws one deterministically
# so its descriptor stays stable over time while the fleet does not report the
# exact same size on every session.
TERMINAL_GEOMETRIES = ((80, 24), (100, 30), (120, 40), (132, 43), (160, 50))


def _terminal_geometry(account_id: str) -> "tuple[int, int]":
    digest = hashlib.sha256(("freebuff:geometry:v1:" + account_id).encode()).digest()
    columns, rows = TERMINAL_GEOMETRIES[digest[0] % len(TERMINAL_GEOMETRIES)]
    return columns, rows

# Complete OSC queries may use BEL or ST (ESC backslash) as their terminator.
TERMINAL_COLOR_QUERY = re.compile(rb"\x1b\](10|11);\?(?:\x07|\x1b\\)")
TERMINAL_COLOR_REPLIES = {
    b"10": b"\x1b]10;rgb:ffff/ffff/ffff\x07",
    b"11": b"\x1b]11;rgb:0000/0000/0000\x07",
}
# The longest supported query is eight bytes; only its prefix can be pending.
TERMINAL_COLOR_QUERY_TAIL = 7

def validate_turn_prompt(prompt, reset_context=True):
    """Validate the complete serialized prompt before any worker is leased."""
    if not isinstance(prompt, str):
        raise FreebuffBadRequestError("Prompt must be text.")
    prompt = prompt.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not prompt:
        raise FreebuffBadRequestError("Prompt must not be empty.")
    if any((ord(char) < 32 and char not in "\n\t") or 127 <= ord(char) <= 159 for char in prompt):
        raise FreebuffBadRequestError("Prompt contains unsupported terminal control characters.")
    try:
        prompt.encode("utf-8")
    except UnicodeEncodeError:
        raise FreebuffBadRequestError("Prompt contains invalid Unicode.") from None
    return prompt


def build_wire_prompt(prompt: str) -> str:
    """Mark serialized requests so command-like text stays ordinary prompt input.

    The pool's serialized format already contains structured sections; prefixing
    it again would duplicate the marker on multi-turn or system-instructed
    requests, a text shape no human session produces.
    """
    if prompt.startswith(("[Current Request]", "[System Instructions]", "[Conversation History]")):
        return prompt
    return "[Current Request]\n" + prompt


def _input_history_signature(path):
    try:
        stat = os.stat(path)
        return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except OSError:
        return None


def _reset_recorded(path, previous_signature):
    """The CLI synchronously saves /new after rotating its in-memory chat ID."""
    signature = _input_history_signature(path)
    if signature is None or signature == previous_signature:
        return False
    try:
        with open(path, encoding="utf-8") as stream:
            history = json.load(stream)
        return isinstance(history, list) and bool(history) and history[-1] == "/new"
    except (OSError, ValueError, UnicodeError):
        return False  # History writes are not atomic; retry partial JSON.


class FreebuffWorker:
    def __init__(
        self,
        workdir: str = WORKER_WORKDIR,
        account_id: str = "default",
        token: Optional[str] = None,
        proxy_url: Optional[str] = None,
        home_dir: Optional[str] = None
    ):
        self.workdir = workdir
        self.account_id = account_id
        self.token = token
        self.proxy_url = proxy_url
        self.home_dir = home_dir or os.path.expanduser(f"~/.freebuff_accounts/{account_id}")
        self.config_dir = account_config_dir(self.home_dir)
        self.project_basename = os.path.basename(os.path.abspath(workdir))
        self.master_fd: Optional[int] = None
        self.pid: Optional[int] = None
        self._supervisor: Optional[asyncio.subprocess.Process] = None
        self._lifetime_fd: Optional[int] = None
        self._exit_task: Optional[asyncio.Task] = None
        self.turn_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self.ready_event = asyncio.Event()
        self.is_running = False
        self._raw_buffer = bytearray()
        self._terminal_query_tail = b""
        self._terminal_reply_lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._reader_task = None
        self._active_chat_dir: Optional[str] = None

        # Keystroke Jitter Simulator (Section III)
        self.jitter_config = JitterConfig(
            mode=JITTER_MODE,
            min_delay_ms=JITTER_MIN_MS,
            max_delay_ms=JITTER_MAX_MS,
            distribution=JITTER_DISTRIBUTION,
            target_max_cps=JITTER_TARGET_MAX_CPS,
            pre_enter_min_ms=PRE_ENTER_MIN_MS,
            pre_enter_max_ms=PRE_ENTER_MAX_MS
        )
        self.jitter_simulator = KeystrokeJitterSimulator(self.jitter_config)
        self.last_jitter_metrics: Optional[JitterMetrics] = None
        self._takeover_scheduled = False
        self._takeover_task = None
        self._last_takeover_time: float = -999999.0
        self._admission_scheduled = False
        self._admission_task = None
        self._admission_retry_count = 0
        self._admission_timestamps: List[float] = []
        self._reply_tasks = set()
        self.is_authenticated = True
        self.auth_error_msg: Optional[str] = None

    def _ensure_git_repo(self):
        """Ensures that workdir is an initialized Git repository with an initial commit."""
        git_dir = os.path.join(self.workdir, ".git")
        if not os.path.exists(git_dir):
            try:
                subprocess.run(["git", "init", "-b", "main"], cwd=self.workdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)
                readme_path = os.path.join(self.workdir, "README.md")
                if not os.path.exists(readme_path):
                    with open(readme_path, "w") as f:
                        f.write("# Workspace\n\nManaged project workspace.\n")

                gitignore_path = os.path.join(self.workdir, ".gitignore")
                if not os.path.exists(gitignore_path):
                    with open(gitignore_path, "w") as f:
                        f.write("node_modules/\n*.pyc\n__pycache__/\n.DS_Store\n")
                # Commit identity is per-account and non-identifying. The host's
                # global git configuration (real name and email) must never be
                # inherited: the CLI's git tools can read every commit author
                # and ship it upstream with the workspace context.
                git_identity = [
                    ["git", "config", "user.name", "Developer"],
                    ["git", "config", "user.email", f"{self.account_id}@workstation.local"],
                ]
                for config_command in git_identity:
                    subprocess.run(config_command, cwd=self.workdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)
                subprocess.run(["git", "add", "."], cwd=self.workdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)
                subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.workdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)
                logger.info(f"Initialized authentic Git workspace in {self.workdir}")
            except Exception as e:
                logger.debug(f"Git repo init error: {e}")

    def prune_old_chats(self, keep: int = 25):
        """Prune only this account's older chat directories."""
        pattern = os.path.join(self.config_dir, f"projects/{self.project_basename}/chats/*")
        dirs = sorted(glob.glob(pattern), key=os.path.getmtime)
        if len(dirs) > keep:
            to_remove = dirs[:-keep]
            import shutil
            for directory in to_remove:
                shutil.rmtree(directory, ignore_errors=True)
            logger.info("Pruned %s old Freebuff chat logs (retained %s).", len(to_remove), keep)

    async def start(self):
        """
        Start a supervised PTY worker tied to this gateway's lifetime.

        Legacy spawn-mode settings use the same supervised lifecycle. The
        lifetime pipe also stops the worker if the gateway is killed abruptly.
        """
        if self._supervisor is not None:
            await self.stop()

        ensure_file_descriptor_limit(4096)
        await asyncio.to_thread(verify_official_cli, FREEBUFF_BIN)

        os.makedirs(self.workdir, exist_ok=True)
        os.makedirs(self.home_dir, exist_ok=True)
        os.makedirs(self.config_dir, exist_ok=True)

        # Use the official inline-token profile format in the account directory.
        write_cli_credentials(self.config_dir, self.token)

        await asyncio.to_thread(self._ensure_git_repo)
        await asyncio.to_thread(self.prune_old_chats, keep=25)
        git_privacy_bin = await asyncio.to_thread(prepare_git_privacy_bin, self.home_dir)
        self._loop = asyncio.get_running_loop()

        # Pin CLI configuration to this account regardless of inherited settings.
        env = get_standard_terminal_env()
        for key in ("FREEBUFF_HARDWARE_PRIVACY", "FREEBUFF_SUPERVISOR_PS_SHIM", "FREEBUFF_SUPERVISOR_TERMINAL_VIEW"):
            if key in os.environ:
                env[key] = os.environ[key]
        if self.home_dir:
            env["HOME"] = self.home_dir
            env["FREEBUFF_CONFIG_DIR"] = self.config_dir
        columns, rows = _terminal_geometry(self.account_id)
        env["COLUMNS"] = str(columns)
        env["LINES"] = str(rows)

        # Accounts authenticate through their isolated credential files. Do not
        # let a host-wide token override the selected account's identity.
        env.pop("CODEBUFF_API_KEY", None)
        env.pop("FREEBUFF_AUTH_TOKEN", None)

        if self.proxy_url:
            env["HTTP_PROXY"] = self.proxy_url
            env["HTTPS_PROXY"] = self.proxy_url
            env["ALL_PROXY"] = self.proxy_url
            env["http_proxy"] = self.proxy_url
            env["https_proxy"] = self.proxy_url
            env["all_proxy"] = self.proxy_url
            env["NO_PROXY"] = ""
            env["no_proxy"] = ""

        args = [FREEBUFF_BIN, "--cwd", self.workdir]
        if os.getenv("FREEBUFF_TRUST_AGENTS", "false").lower() == "true":
            args.append("--trust-agents")
        # Login profiles can replace PATH; apply the guard after they finish.
        # Telemetry is refused by the gateway proxy, not by the CLI itself;
        # /api/logs shipping stays off through the upstream-supported switch.
        prelude = (
            "export CODEBUFF_SHIP_LOGS=false; "
            f"export PATH={shlex.quote(git_privacy_bin)}:\"$PATH\"; " + RESTORE_SHIM_PATH
        )
        command = await asyncio.to_thread(
            build_worker_command, args, env, self.account_id, SHELL_PATH, USE_LOGIN_SHELL, prelude,
        )

        # Allocate PTY/lease descriptors only after the privacy probe succeeds.
        master, slave = pty.openpty()
        os.set_blocking(master, False)
        winsize = struct.pack("HHHH", rows, columns, 0, 0)
        fcntl.ioctl(master, termios.TIOCSWINSZ, winsize)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, winsize)
        attrs = termios.tcgetattr(slave)
        attrs[0] &= ~termios.ICRNL
        attrs[3] &= ~termios.ICANON
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        control_read, self._lifetime_fd = os.pipe()
        self.master_fd = master
        self.ready_event.clear()
        self._raw_buffer.clear()
        self._terminal_query_tail = b""
        self.is_authenticated = True
        self.auth_error_msg = None
        self._takeover_scheduled = False
        self._takeover_task = None
        self._admission_scheduled = False
        self._admission_task = None
        self._admission_retry_count = 0
        try:
            self._supervisor = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).with_name("worker_supervisor.py")),
                str(control_read), str(slave), *command,
                pass_fds=(control_read, slave),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=None,
                env=env,
            )
        except BaseException:
            await self.stop()
            raise
        finally:
            os.close(control_read)
            os.close(slave)
        try:
            line = await asyncio.wait_for(self._supervisor.stdout.readline(), timeout=5)
            if not line:
                raise RuntimeError("Freebuff supervisor exited before starting worker")
            self.pid = int(line.strip())
            self.is_running = True
            self._loop.add_reader(master, self._on_pty_readable)
            self._exit_task = asyncio.create_task(self._watch_supervisor(self._supervisor))
        except BaseException:
            await self.stop()
            raise
        logger.info(
            "Spawned supervised Freebuff worker (pid=%s, supervisor=%s, spawn_mode=%s)",
            self.pid, self._supervisor.pid, WORKER_SPAWN_MODE,
        )

        # Wait for worker to boot and report ready
        try:
            await asyncio.wait_for(self._wait_for_ready(), timeout=SESSION_BOOT_TIMEOUT_SECONDS)
            logger.info("Freebuff worker is warm, ready, and admitted into session.")
        except asyncio.TimeoutError:
            await self.stop()
            raise FreebuffTimeoutError("CLI did not become ready before the boot deadline.")
        except BaseException:
            await self.stop()
            raise

    async def _watch_supervisor(self, supervisor):
        await supervisor.wait()
        if self._supervisor is supervisor:
            self.is_running = False
            self.ready_event.set()
            self._close_terminal()

    def _close_terminal(self):
        self._terminal_query_tail = b""
        for task in tuple(self._reply_tasks):
            task.cancel()
        if self.master_fd is not None:
            if self._loop and not self._loop.is_closed():
                self._loop.remove_reader(self.master_fd)
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

    def get_freebuff_pid(self) -> Optional[int]:
        """Finds the PID of the active Freebuff process running under zsh."""
        if not self.pid:
            return None
        try:
            out = subprocess.check_output(
                ["pgrep", "-P", str(self.pid)],
                stderr=subprocess.DEVNULL
            ).decode().strip().split()
            if out:
                return int(out[0])
        except Exception:
            pass
        return self.pid

    def _on_pty_readable(self):
        """Callback when PTY master has bytes available."""
        if self.master_fd is None:
            return
        try:
            chunk = os.read(self.master_fd, 8192)
            if not chunk:
                self.is_running = False
                self.ready_event.set()
                self._close_terminal()
                return
            self._raw_buffer.extend(chunk)
            if len(self._raw_buffer) > 65536:
                self._raw_buffer = self._raw_buffer[-65536:]

            self._respond_to_terminal_color_queries(chunk)

            text_lower = self._raw_buffer.lower()

            # Check for fatal account/network modals to unblock waiters immediately
            fatal_indicators = (
                b"this account is suspended",
                b"disable vpn",
                b"could not verify your location",
                b"freebuff is unavailable in",
                b"not enough hours",
                b"session limit reached",
                b"too many freebuff sessions on this network",
            )
            if any(ind in text_lower for ind in fatal_indicators):
                self.ready_event.set()

            # Check for takeover modal prompt
            if TAKEOVER_AUTO_CONFIRM:
                if b"take over" in text_lower or b"takeover" in text_lower:
                    now = time.monotonic()
                    if now - self._last_takeover_time < TAKEOVER_COOLDOWN_SECONDS:
                        if not self._takeover_scheduled:
                            logger.warning(
                                "Repeated takeover dialog within cooldown window (%.1fs < %ds) for account '%s'. "
                                "Refusing takeover to prevent upstream ping-pong ban.",
                                now - self._last_takeover_time,
                                TAKEOVER_COOLDOWN_SECONDS,
                                self.account_id,
                            )
                            self.ready_event.set()
                    else:
                        if not self._takeover_scheduled:
                            self._takeover_scheduled = True
                            self._last_takeover_time = now
                            logger.info("Detected takeover dialog. Scheduling cognitive delay before confirming...")
                            self._takeover_task = asyncio.create_task(self._delayed_takeover_confirm())

            # Check for admission confirmation modal (model change, wallet hour deduction, retry)
            if ADMISSION_AUTO_CONFIRM:
                if (
                    b"confirm and send" in text_lower
                    or b"enter: confirm" in text_lower
                    or b"enter: retry" in text_lower
                ):
                    if not self._admission_scheduled:
                        if b"enter: retry" in text_lower:
                            self._admission_retry_count += 1
                            if self._admission_retry_count > 2:
                                logger.warning("Admission retry limit reached; not auto-retrying.")
                            else:
                                self._admission_scheduled = True
                                logger.info("Detected admission retry dialog. Scheduling cognitive delay before retrying...")
                                self._admission_task = asyncio.create_task(self._delayed_admission_confirm())
                        else:
                            now = time.monotonic()
                            self._admission_timestamps = [t for t in self._admission_timestamps if now - t < 3600.0]
                            if len(self._admission_timestamps) >= MAX_HOURLY_ADMISSION:
                                logger.warning(
                                    "Hourly admission/spend confirmation limit reached (%d/%d) for account '%s'. "
                                    "Pausing auto-confirm to protect quota.",
                                    len(self._admission_timestamps),
                                    MAX_HOURLY_ADMISSION,
                                    self.account_id,
                                )
                                self.ready_event.set()
                            else:
                                self._admission_timestamps.append(now)
                                self._admission_scheduled = True
                                logger.info("Detected admission confirmation dialog. Scheduling cognitive delay before confirming...")
                                self._admission_task = asyncio.create_task(self._delayed_admission_confirm())

            # Check for login prompt (unauthenticated / expired token)
            if b"Press ENTER to login" in self._raw_buffer or b"Press enter to login" in self._raw_buffer:
                self.is_authenticated = False
                self.auth_error_msg = "Freebuff authentication required: token is invalid or expired. Run ./login.sh to authenticate."
                self.ready_event.set()

            # Check for ready prompt
            if not self.ready_event.is_set():
                if (b"hours remaining" in self._raw_buffer or
                    b"Enter a coding task" in self._raw_buffer or
                    b"Add to the current task" in self._raw_buffer):
                    self.ready_event.set()

        except BlockingIOError:
            return
        except OSError:
            # An exited PTY stays readable (EIO on Linux); remove the reader
            # instead of spinning the event loop indefinitely.
            self.is_running = False
            self.ready_event.set()
            self._close_terminal()

    def _respond_to_terminal_color_queries(self, chunk):
        """Answer complete queries once, including ones split across PTY reads."""
        data = self._terminal_query_tail + chunk
        consumed = 0
        replies = bytearray()
        for match in TERMINAL_COLOR_QUERY.finditer(data):
            replies.extend(TERMINAL_COLOR_REPLIES[match[1]])
            consumed = match.end()
        # Never replay matched queries or retain unbounded terminal output.
        self._terminal_query_tail = data[consumed:][-TERMINAL_COLOR_QUERY_TAIL:]
        if replies:
            self._schedule_terminal_reply(bytes(replies))

    def _schedule_terminal_reply(self, payload):
        fd = self.master_fd
        if fd is None:
            return
        async def reply():
            try:
                # Keep responses ordered even if a nonblocking write must wait.
                async with self._terminal_reply_lock:
                    if self.master_fd == fd:
                        await write_all(fd, payload)
            except OSError:
                pass
        task = asyncio.create_task(reply())
        self._reply_tasks.add(task)
        task.add_done_callback(self._reply_tasks.discard)

    async def _delayed_takeover_confirm(self):
        """Simulates human visual reaction and reading delay (450ms-850ms) before pressing Enter."""
        await asyncio.sleep(random.uniform(0.45, 0.85))
        if TAKEOVER_AUTO_CONFIRM and self.master_fd is not None:
            try:
                await write_all(self.master_fd, b"\r")
                logger.info("Confirmed explicitly enabled session takeover.")
            except OSError:
                pass
        self._raw_buffer.clear()
        self._takeover_scheduled = False

    async def _delayed_admission_confirm(self):
        """Simulates human visual reaction and reading delay (450ms-850ms) before pressing Enter on admission dialogs."""
        await asyncio.sleep(random.uniform(0.45, 0.85))
        if ADMISSION_AUTO_CONFIRM and self.master_fd is not None:
            try:
                await write_all(self.master_fd, b"\r")
                logger.info("Confirmed explicitly enabled model admission / spend confirmation.")
            except OSError:
                pass
        self._raw_buffer.clear()
        self._admission_scheduled = False

    async def _wait_for_ready(self):
        """Polls until ready_event is set or login prompt detected."""
        while not self.ready_event.is_set():
            self._check_interaction()
            if not self.is_running:
                raise RuntimeError("Freebuff worker exited during startup")
            if b"Press ENTER to login" in self._raw_buffer or b"Press enter to login" in self._raw_buffer:
                self.is_authenticated = False
                self.auth_error_msg = "Freebuff authentication required: token is invalid or expired. Run ./login.sh to authenticate."
                self.ready_event.set()
                break
            await asyncio.sleep(0.05)
        self._check_interaction()
        if not self.is_running:
            raise RuntimeError("Freebuff worker exited during startup")

    def _get_project_chat_dir(self) -> Optional[str]:
        """Find the newest chat in this worker's account configuration only."""
        pattern = os.path.join(self.config_dir, f"projects/{self.project_basename}/chats/*")
        dirs = sorted(glob.glob(pattern), key=os.path.getmtime)
        return dirs[-1] if dirs else None

    async def reset_chat(self):
        """Sends /new with keystroke pacing to clear previous chat session context."""
        if not self.is_running or self.master_fd is None:
            return
        logger.info("Resetting Freebuff chat context via /new")
        await self.jitter_simulator.type_into_fd(self.master_fd, "/new", append_return=True)
        await asyncio.sleep(0.5)

    def current_model_key(self) -> Optional[str]:
        """Return the model key persisted for this account, if any."""
        try:
            with open(os.path.join(self.config_dir, "settings.json"), "r") as f:
                return json.load(f).get("freebuffModelKey")
        except (OSError, ValueError):
            return None

    async def ensure_model(self, target_model_key: str):
        """
        Ensures that the Freebuff worker is configured with the target model key.
        If it differs from current settings, updates settings.json and restarts the worker.
        """
        if not target_model_key:
            return

        settings_path = os.path.join(self.config_dir, "settings.json")
        current_key = None
        settings_data = {}
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r") as f:
                    settings_data = json.load(f)
                    current_key = settings_data.get("freebuffModelKey")
            except Exception as e:
                logger.warning(f"Failed to read settings.json: {e}")

        if current_key != target_model_key:
            logger.info(f"Switching Freebuff model from {current_key} to {target_model_key}...")
            settings_data["freebuffModelKey"] = target_model_key
            try:
                with open(settings_path, "w") as f:
                    json.dump(settings_data, f, indent=2)
            except Exception as e:
                raise FreebuffServerError("Could not update CLI model settings.") from e

            # Restart worker to apply model selection
            await self.stop()
            await self.start()

    async def ensure_workspace(self, target_workspace: str):
        """
        Ensures that the Freebuff worker is running inside the requested workspace.
        If it differs from current workdir, restarts worker in the new workspace so Freebuff
        binds to the genuine project Git repository and codebase.
        """
        if not target_workspace:
            return

        from .workspace import validate_workspace_path
        normalized = validate_workspace_path(target_workspace)
        if not os.path.isdir(normalized):
            raise FreebuffBadRequestError("Workspace directory no longer exists.", "invalid_workspace")

        from .workspace import is_safe_isolated_path
        if not is_safe_isolated_path(normalized):
            raise FreebuffPermissionError("Workspace is outside the configured allowed roots.", "workspace_not_allowed")

        if os.path.abspath(self.workdir) != normalized:
            logger.info(f"Switching Freebuff workspace from '{self.workdir}' to '{normalized}'...")
            self.workdir = normalized
            self.project_basename = os.path.basename(normalized)
            # Restart worker to mount in the new directory
            await self.stop()
            await self.start()

    def _check_interaction(self):
        text = strip_ansi(bytes(self._raw_buffer)).lower()
        if not self.is_authenticated or "press enter to login" in text:
            raise FreebuffAuthError()
        if "this account is suspended" in text:
            self.is_authenticated = False
            raise FreebuffAuthError("This Freebuff account has been suspended by upstream.", error_code="account_suspended")
        if "disable vpn, proxy or tor traffic" in text or "disable vpn" in text:
            raise FreebuffPermissionError(
                "Freebuff detected anonymous network / VPN / Tor traffic. Change or disable your proxy node.",
                error_code="anonymous_network_blocked",
            )
        if "could not verify your location" in text or "freebuff is unavailable in" in text:
            raise FreebuffPermissionError(
                "Freebuff is unavailable in your region or could not verify location.",
                error_code="country_blocked",
            )
        if "not enough hours" in text or "session limit reached" in text:
            raise FreebuffPaymentError(
                "Freebuff hours / credits exhausted for this account.",
                error_code="insufficient_quota",
            )
        if "too many freebuff sessions on this network" in text:
            raise FreebuffRateLimitError(
                "Too many Freebuff sessions on this network. Please retry later.",
                error_code="ip_capped",
                retry_after=30.0,
            )
        if "this session was taken over elsewhere" in text:
            raise FreebuffInteractionRequiredError(
                "This Freebuff session was taken over elsewhere.",
                error_code="session_superseded",
            )
        if ("take over" in text or "takeover" in text):
            if not TAKEOVER_AUTO_CONFIRM:
                raise FreebuffInteractionRequiredError("CLI session takeover requires explicit user action.")
            now = time.monotonic()
            if now - self._last_takeover_time < TAKEOVER_COOLDOWN_SECONDS and not self._takeover_scheduled:
                raise FreebuffInteractionRequiredError(
                    f"Session takeover refused: repeated takeover within {TAKEOVER_COOLDOWN_SECONDS}s cooldown. "
                    "Another active client may be using this account.",
                    error_code="takeover_cooldown",
                )
        if ("confirm and send" in text or "enter: confirm" in text):
            if not ADMISSION_AUTO_CONFIRM:
                raise FreebuffInteractionRequiredError("CLI model admission requires explicit user confirmation.")
            now = time.monotonic()
            recent_admissions = [t for t in self._admission_timestamps if now - t < 3600.0]
            if len(recent_admissions) >= MAX_HOURLY_ADMISSION and not self._admission_scheduled:
                raise FreebuffInteractionRequiredError(
                    f"Hourly model admission/spend confirmation limit ({MAX_HOURLY_ADMISSION}/hr) reached. "
                    "Auto-confirm paused to protect account quota.",
                    error_code="admission_rate_limited",
                )
        if "load and run these? [y/n]" in text:
            raise FreebuffInteractionRequiredError()

    async def execute_turn(self, prompt, reset_context=True, on_chunk=None):
        """Run one bounded turn and return only its correlated public response."""
        prompt = validate_turn_prompt(prompt, reset_context)
        try:
            await asyncio.wait_for(self.turn_lock.acquire(), TURN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise FreebuffRateLimitError("Worker is busy.", error_code="queue_timeout")
        try:
            try:
                return await asyncio.wait_for(
                    self._execute_turn(prompt, reset_context, on_chunk), TURN_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                raise FreebuffTimeoutError() from None
            finally:
                # A partial turn must never survive into another request.
                if not getattr(self, "_turn_succeeded", False):
                    cleanup = asyncio.create_task(self.stop())
                    cancelled = False
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            # A disconnect may race the pool's deadline. Keep
                            # the lease until cleanup survives both cancels.
                            cancelled = True
                        except Exception:
                            break
                    if not cleanup.cancelled() and cleanup.exception() is not None:
                        logger.error("Worker cleanup failed after an unsuccessful turn.")
                    if cancelled or cleanup.cancelled():
                        raise asyncio.CancelledError
        finally:
            self._turn_succeeded = False
            self.turn_lock.release()

    async def _execute_turn(self, prompt, reset_context, on_chunk):
        self._turn_succeeded = False
        if not self.is_running or self.master_fd is None:
            raise FreebuffServerError("Freebuff worker is not running.")
        self._check_interaction()
        await self.ready_event.wait()
        self._check_interaction()
        before = await asyncio.to_thread(self._get_project_chat_dir)
        pattern = os.path.join(self.config_dir, "projects", self.project_basename, "chats", "*")
        existing = set(await asyncio.to_thread(glob.glob, pattern))
        if before:
            existing.add(before)
        reader = TranscriptReader()
        # Fresh-directory isolation makes the previous chat irrelevant on /new.
        # Reuse this reader for a retained chat so its baseline is parsed once.
        baseline = [] if reset_context else (await asyncio.to_thread(reader.read_changed, before) or [])
        baseline_ids = {m.get("id") for m in baseline if m.get("id")}
        del baseline
        positions = {}
        if before:
            try:
                positions[before] = os.path.getsize(os.path.join(before, "log.jsonl"))
            except OSError:
                positions[before] = 0
        self._raw_buffer.clear()
        # Keep CLI command and file-path detection from interpreting API text.
        wire_prompt = build_wire_prompt(prompt)
        history_path = os.path.join(self.config_dir, "message-history.json")
        history_signature = await asyncio.to_thread(_input_history_signature, history_path) if reset_context else None
        # A long paste becomes an attachment, so /new must be submitted on
        # its own and finish rotating the chat before the prompt is pasted.
        prompt_submitted = not reset_context
        if reset_context:
            await self.jitter_simulator.type_into_fd(self.master_fd, "/new", append_return=True)
        else:
            self.last_jitter_metrics = await self.jitter_simulator.paste_into_fd(
                self.master_fd, wire_prompt, append_return=True
            )
        emitted = {"content": "", "reasoning_content": ""}
        bound_dir = None
        bound_identity = None
        while True:
            if not self.is_running or self.master_fd is None:
                raise FreebuffServerError("CLI exited before completing the current turn.")
            self._check_interaction()
            directory = await asyncio.to_thread(self._get_project_chat_dir)
            if bound_dir and directory != bound_dir:
                raise FreebuffServerError("CLI changed chat directories during this turn.")
            if not prompt_submitted and (not directory or directory in existing):
                if before:
                    position, records = await asyncio.to_thread(
                        read_log_records, os.path.join(before, "log.jsonl"), positions.get(before, 0)
                    )
                    positions[before] = position
                    for record in records:
                        check_log_error(record)
                # /new may only rotate an in-memory ID: getCurrentChatDir()
                # creates its directory lazily when the next message is sent.
                if await asyncio.to_thread(_reset_recorded, history_path, history_signature):
                    self.last_jitter_metrics = await self.jitter_simulator.paste_into_fd(
                        self.master_fd, wire_prompt, append_return=True
                    )
                    prompt_submitted = True
                    continue
                await asyncio.sleep(0.15)
                continue
            if not directory:
                await asyncio.sleep(0.15)
                continue
            if reset_context and directory in existing:
                if directory == before:
                    position, records = await asyncio.to_thread(
                        read_log_records, os.path.join(directory, "log.jsonl"), positions.get(directory, 0)
                    )
                    positions[directory] = position
                    for record in records:
                        check_log_error(record)
                await asyncio.sleep(0.15)
                continue
            if not reset_context and directory in existing and directory != before:
                await asyncio.sleep(0.15)
                continue
            position, records = await asyncio.to_thread(
                read_log_records, os.path.join(directory, "log.jsonl"), positions.get(directory, 0)
            )
            positions[directory] = position
            for record in records:
                check_log_error(record)
                # End-agent/step logs and chat-meta summaries are not full-turn
                # completion. Public isComplete below is set by handleRunCompletion.
                if record.get("msg") == "Main prompt finished":
                    data = record.get("data") or {}
                    if data.get("outputType") == "error":
                        raise FreebuffServerError("CLI prompt execution failed.")
            if not prompt_submitted:
                # Bind before submission, even if the first response has not
                # been persisted yet. Pre-existing reset notices are baseline.
                reset_messages = await asyncio.to_thread(reader.read_changed, directory) or []
                baseline_ids.update(m.get("id") for m in reset_messages if m.get("id"))
                del reset_messages
                bound_dir = directory
                self.last_jitter_metrics = await self.jitter_simulator.paste_into_fd(
                    self.master_fd, wire_prompt, append_return=True
                )
                prompt_submitted = True
                continue
            if reset_context and bound_dir is None:
                bound_dir = directory
            messages = await asyncio.to_thread(reader.read_changed, directory)
            if messages is not None:
                response = current_response(messages, baseline_ids, wire_prompt)
                del messages
                if response:
                    identity = response["user_id"], response["id"]
                    if bound_identity is not None and identity != bound_identity:
                        raise FreebuffServerError("CLI changed message identities during this turn.")
                    bound_identity = identity
                    bound_dir = directory
                    for key in ("reasoning_content", "content"):
                        value = response[key]
                        if not value.startswith(emitted[key]):
                            raise FreebuffServerError("CLI revised output already sent to the client.")
                        suffix = value[len(emitted[key]):]
                        if suffix and on_chunk:
                            on_chunk(suffix, key == "reasoning_content")
                        emitted[key] = value
                    if response["complete"]:
                        self._active_chat_dir = directory
                        self._turn_succeeded = True
                        self._admission_retry_count = 0
                        return {"content": response["content"],
                            "reasoning_content": response["reasoning_content"] or None,
                            "chat_dir": directory,
                            "message_id": response["id"],
                            "jitter_metrics": self.last_jitter_metrics.to_dict()}
            await asyncio.sleep(0.15)

    async def stop(self):
        """Close the PTY even when a broken supervisor fails to acknowledge exit."""
        async with self._stop_lock:
            await self._stop()

    async def _stop(self):
        self.is_running = False
        self.ready_event.set()
        try:
            if self._lifetime_fd is not None:
                os.close(self._lifetime_fd)
                self._lifetime_fd = None
            if self._supervisor is not None:
                await asyncio.wait_for(asyncio.shield(self._supervisor.wait()), timeout=5)
        finally:
            self._close_terminal()
            for task in (self._exit_task, self._takeover_task, self._admission_task, *self._reply_tasks):
                if task and task is not asyncio.current_task():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self._reply_tasks.clear()
            self._exit_task = None
            self._takeover_task = None
            self._takeover_scheduled = False
            self._admission_task = None
            self._admission_scheduled = False
            self._admission_retry_count = 0
            if self._supervisor is None or self._supervisor.returncode is not None:
                self._supervisor = None
                self.pid = None
