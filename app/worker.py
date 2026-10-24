import os
import pty
import termios
import fcntl
import struct
import random
import asyncio
import logging
import json
import glob
import re
import sys
import subprocess
from typing import Optional, Callable, Dict, Any, List

from .config import (
    FREEBUFF_BIN,
    WORKER_WORKDIR,
    TURN_TIMEOUT_SECONDS,
    SESSION_BOOT_TIMEOUT_SECONDS,
    TAKEOVER_AUTO_CONFIRM,
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
from .jitter import KeystrokeJitterSimulator, JitterConfig, JitterMetrics
from .exceptions import (
    FreebuffException,
    FreebuffAuthError,
    FreebuffRateLimitError,
    FreebuffPermissionError,
    FreebuffServerError,
    FreebuffTimeoutError
)

logger = logging.getLogger("freebuff.worker")

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
        self.project_basename = os.path.basename(os.path.abspath(workdir))
        self.master_fd: Optional[int] = None
        self.pid: Optional[int] = None
        self.turn_lock = asyncio.Lock()
        self.ready_event = asyncio.Event()
        self.is_running = False
        self._raw_buffer = bytearray()
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
        self.is_authenticated = True
        self.auth_error_msg: Optional[str] = None

    def _ensure_git_repo(self):
        """Ensures that workdir is an initialized Git repository with an initial commit."""
        git_dir = os.path.join(self.workdir, ".git")
        if not os.path.exists(git_dir):
            try:
                subprocess.run(["git", "init", "-b", "main"], cwd=self.workdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                readme_path = os.path.join(self.workdir, "README.md")
                if not os.path.exists(readme_path):
                    with open(readme_path, "w") as f:
                        f.write("# Workspace\n\nManaged project workspace.\n")
                gitignore_path = os.path.join(self.workdir, ".gitignore")
                if not os.path.exists(gitignore_path):
                    with open(gitignore_path, "w") as f:
                        f.write("node_modules/\n*.pyc\n__pycache__/\n.DS_Store\n")
                # Inherit global git configuration if present; only provide fallback if completely unconfigured
                has_name = subprocess.run(["git", "config", "user.name"], cwd=self.workdir, capture_output=True, text=True, check=False).stdout.strip()
                if not has_name:
                    sys_user = os.environ.get("USER") or "Developer"
                    subprocess.run(["git", "config", "user.name", sys_user], cwd=self.workdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

                has_email = subprocess.run(["git", "config", "user.email"], cwd=self.workdir, capture_output=True, text=True, check=False).stdout.strip()
                if not has_email:
                    sys_user = os.environ.get("USER") or "dev"
                    subprocess.run(["git", "config", "user.email", f"{sys_user}@local"], cwd=self.workdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                subprocess.run(["git", "add", "."], cwd=self.workdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.workdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                logger.info(f"Initialized authentic Git workspace in {self.workdir}")
            except Exception as e:
                logger.debug(f"Git repo init error: {e}")

    def prune_old_chats(self, keep: int = 25):
        """Prunes older chat directories in ~/.config/manicode/projects/<basename>/chats/ to prevent disk bloat."""
        pattern = os.path.join(self.home_dir, f".config/manicode/projects/{self.project_basename}/chats/*")
        dirs = sorted(glob.glob(pattern), key=os.path.getmtime)
        if len(dirs) > keep:
            to_remove = dirs[:-keep]
            import shutil
            for d in to_remove:
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass
            logger.info(f"Pruned {len(to_remove)} old Freebuff chat logs (retained {keep} most recent).")

    async def start(self):
        """
        Starts the Freebuff CLI in an isolated, authentic pseudo-terminal (PTY).
        Applies double-fork daemon reparenting to launchd (PID 1) and executes via zsh -l.
        Guarantees process chain shows p=shell, eliminating p=python completely.
        """
        os.makedirs(self.workdir, exist_ok=True)
        os.makedirs(self.home_dir, exist_ok=True)

        # Seed isolated account token if provided
        if self.token:
            creds_dir = os.path.join(self.home_dir, ".config", "manicode")
            os.makedirs(creds_dir, exist_ok=True)
            creds_file = os.path.join(creds_dir, "credentials.json")
            if not os.path.exists(creds_file):
                try:
                    with open(creds_file, "w") as f:
                        json.dump({"accessToken": self.token, "tokenStore": "file"}, f, indent=2)
                except Exception as e:
                    logger.debug(f"Failed to seed credentials in {creds_file}: {e}")

        self._ensure_git_repo()
        self.prune_old_chats(keep=25)
        self._loop = asyncio.get_running_loop()
        
        master, slave = pty.openpty()
        # Set physical terminal window geometry (40 rows, 120 columns)
        # Prevents client-environment from detecting sz=0x0
        winsize = struct.pack("HHHH", 40, 120, 0, 0)
        fcntl.ioctl(master, termios.TIOCSWINSZ, winsize)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, winsize)

        # Configure terminal attributes: raw mode, no ICRNL translation, no local echo
        attrs = termios.tcgetattr(slave)
        attrs[0] &= ~termios.ICRNL
        attrs[3] &= ~termios.ICANON
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)

        # Prepare authentic terminal environment (Section IV)
        env = get_standard_terminal_env()
        if self.home_dir:
            env["HOME"] = self.home_dir

        if self.proxy_url:
            env["HTTP_PROXY"] = self.proxy_url
            env["HTTPS_PROXY"] = self.proxy_url
            env["ALL_PROXY"] = self.proxy_url
            env["http_proxy"] = self.proxy_url
            env["https_proxy"] = self.proxy_url
            env["all_proxy"] = self.proxy_url

        pipe_r, pipe_w = os.pipe()

        if WORKER_SPAWN_MODE == "detached_zsh":
            # Section I & II: Double-fork daemon reparenting to launchd (PID 1)
            pid1 = os.fork()
            if pid1 == 0:
                os.close(pipe_r)
                pid2 = os.fork()
                if pid2 == 0:
                    # Grandchild process: become independent session leader
                    os.close(master)
                    os.setsid()
                    os.dup2(slave, 0)
                    os.dup2(slave, 1)
                    os.dup2(slave, 2)
                    os.close(slave)

                    # Acquire controlling terminal on BSD/macOS
                    try:
                        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
                    except Exception:
                        pass

                    # Report grandchild PID to API process
                    my_pid = os.getpid()
                    os.write(pipe_w, f"{my_pid}\n".encode())
                    os.close(pipe_w)

                    # Execute via authentic interactive login shell
                    # Note: appending '; exit $?' prevents zsh from auto-exec'ing, ensuring zsh stays as parent process
                    # Check if repo has .agents before appending trust flag to avoid unnecessary CI signature
                    trust_flag = " --trust-agents" if os.path.exists(os.path.join(self.workdir, ".agents")) else ""
                    freebuff_cmd = f"'{FREEBUFF_BIN}' --cwd '{self.workdir}'{trust_flag}; exit $?"
                    os.execve(SHELL_PATH, ["zsh", "-l", "-c", freebuff_cmd], env)
                else:
                    # Intermediate child exits immediately without running asyncio event loop cleanup
                    os.close(pipe_w)
                    os._exit(0)

            os.close(slave)
            os.close(pipe_w)
            os.waitpid(pid1, 0)
            
            # Read grandchild PID
            pid_raw = os.read(pipe_r, 64).decode().strip()
            self.pid = int(pid_raw) if pid_raw else None
            os.close(pipe_r)
        else:
            # Direct fork under zsh -l
            pid = os.fork()
            if pid == 0:
                os.close(master)
                os.setsid()
                os.dup2(slave, 0)
                os.dup2(slave, 1)
                os.dup2(slave, 2)
                os.close(slave)
                trust_flag = " --trust-agents" if os.path.exists(os.path.join(self.workdir, ".agents")) else ""
                freebuff_cmd = f"'{FREEBUFF_BIN}' --cwd '{self.workdir}'{trust_flag}"
                os.execve(SHELL_PATH, ["zsh", "-l", "-c", freebuff_cmd], env)

            os.close(slave)
            os.close(pipe_r)
            os.close(pipe_w)
            self.pid = pid

        self.master_fd = master
        self.is_running = True
        self.ready_event.clear()
        self._raw_buffer.clear()

        # Register event loop reader
        self._loop.add_reader(self.master_fd, self._on_pty_readable)
        logger.info(
            f"Spawned detached Freebuff worker under zsh (grandchild pid={self.pid}) on master_fd={master} "
            f"[spawn_mode={WORKER_SPAWN_MODE}, jitter_mode={self.jitter_config.mode}]"
        )

        # Wait for worker to boot and report ready
        try:
            await asyncio.wait_for(self._wait_for_ready(), timeout=SESSION_BOOT_TIMEOUT_SECONDS)
            logger.info("Freebuff worker is warm, ready, and admitted into session.")
        except asyncio.TimeoutError:
            logger.warning("Freebuff boot timeout, proceeding with best effort.")
            self.ready_event.set()

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
                return
            self._raw_buffer.extend(chunk)
            if len(self._raw_buffer) > 65536:
                self._raw_buffer = self._raw_buffer[-65536:]

            # Respond to OSC 10 / OSC 11 color queries from Freebuff
            # Prevents 2000ms startup timeout and ensures osc=1 in x-freebuff-env
            if b"\x1b]11;?" in chunk:
                # Terminal background color query (OSC 11) -> Reply dark background #000000
                os.write(self.master_fd, b"\x1b]11;rgb:0000/0000/0000\x07")
            if b"\x1b]10;?" in chunk:
                # Terminal foreground color query (OSC 10) -> Reply light foreground #ffffff
                os.write(self.master_fd, b"\x1b]10;rgb:ffff/ffff/ffff\x07")

            # Check for takeover modal prompt
            if TAKEOVER_AUTO_CONFIRM:
                text_lower = self._raw_buffer.lower()
                if b"take over" in text_lower or b"takeover" in text_lower:
                    if not self._takeover_scheduled:
                        self._takeover_scheduled = True
                        logger.info("Detected takeover dialog. Scheduling cognitive delay before confirming...")
                        asyncio.create_task(self._delayed_takeover_confirm())

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

        except OSError:
            pass

    async def _delayed_takeover_confirm(self):
        """Simulates human visual reaction and reading delay (450ms-850ms) before pressing Enter."""
        await asyncio.sleep(random.uniform(0.45, 0.85))
        if self.master_fd is not None:
            try:
                os.write(self.master_fd, b"\r")
                logger.info("Confirmed takeover after human hesitation pause.")
            except OSError:
                pass
        self._raw_buffer.clear()
        self._takeover_scheduled = False

    async def _wait_for_ready(self):
        """Polls until ready_event is set or login prompt detected."""
        while not self.ready_event.is_set():
            if b"Press ENTER to login" in self._raw_buffer or b"Press enter to login" in self._raw_buffer:
                self.is_authenticated = False
                self.auth_error_msg = "Freebuff authentication required: token is invalid or expired. Run ./login.sh to authenticate."
                self.ready_event.set()
                break
            await asyncio.sleep(0.05)

    def _get_project_chat_dir(self) -> Optional[str]:
        """Finds the most recent chat directory for this worker's workspace."""
        pattern = os.path.join(self.home_dir, f".config/manicode/projects/{self.project_basename}/chats/*")
        dirs = sorted(glob.glob(pattern), key=os.path.getmtime)
        return dirs[-1] if dirs else None

    async def reset_chat(self):
        """Sends /new with keystroke pacing to clear previous chat session context."""
        if not self.is_running or self.master_fd is None:
            return
        logger.info("Resetting Freebuff chat context via /new")
        await self.jitter_simulator.type_into_fd(self.master_fd, "/new", append_return=True)
        await asyncio.sleep(0.5)

    async def ensure_model(self, target_model_key: str):
        """
        Ensures that the Freebuff worker is configured with the target model key.
        If it differs from current settings, updates settings.json and restarts the worker.
        """
        if not target_model_key:
            return

        settings_path = os.path.join(self.home_dir, ".config/manicode/settings.json")
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
                logger.error(f"Failed to update settings.json: {e}")
                return

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

        normalized = os.path.abspath(os.path.expanduser(target_workspace))
        if not os.path.isdir(normalized):
            logger.warning(f"Target workspace does not exist on disk: {normalized}")
            return

        from .workspace import is_safe_isolated_path
        if not is_safe_isolated_path(normalized):
            logger.warning(f"🛡️ [Code-level Guard] Refusing to switch Freebuff worker to sensitive host path: {normalized}")
            return

        if os.path.abspath(self.workdir) != normalized:
            logger.info(f"Switching Freebuff workspace from '{self.workdir}' to '{normalized}'...")
            self.workdir = normalized
            self.project_basename = os.path.basename(normalized)
            # Restart worker to mount in the new directory
            await self.stop()
            await self.start()

    async def execute_turn(
        self,
        prompt: str,
        reset_context: bool = True,
        on_chunk: Optional[Callable[[str, bool], None]] = None
    ) -> Dict[str, Any]:
        """
        Executes an inference turn by typing the prompt into the PTY session using Keystroke Jitter.
        Simulates authentic inter-keystroke intervals, pre-Enter pauses, and measures exact composeMs.
        """
        async with self.turn_lock:
            if not self.is_running or self.master_fd is None:
                raise RuntimeError("Freebuff worker is not running")

            await self.ready_event.wait()

            # Fast-fail immediately if worker detected unauthenticated state or login prompt
            if not self.is_authenticated or b"Press ENTER to login" in self._raw_buffer or b"Press enter to login" in self._raw_buffer:
                raise FreebuffAuthError(self.auth_error_msg or "Freebuff authentication required: token is invalid or expired. Run ./login.sh to authenticate.")

            chat_dir_before = self._get_project_chat_dir()
            log_pos = 0
            if chat_dir_before:
                log_file = os.path.join(chat_dir_before, "log.jsonl")
                if os.path.exists(log_file):
                    try:
                        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                            recent_logs = f.read()
                            if "API key validation failed" in recent_logs or "getUserInfoFromApiKey authentication failed" in recent_logs:
                                self.is_authenticated = False
                                raise FreebuffAuthError("Freebuff API authentication failed: token expired or invalid in Keychain. Run ./login.sh to re-authenticate.")
                    except FreebuffAuthError:
                        raise
                    except Exception:
                        pass
                    log_pos = os.path.getsize(log_file)

            if "\n" in prompt or len(prompt) > 100:
                # Section III.B: Multi-line or long prompt: use authentic Bracketed Paste mode
                # Legitimate human workflow for code blocks/large text; sets pe=1, pc=N in input_profile
                if reset_context:
                    # Clear session context first via /new
                    await self.jitter_simulator.type_into_fd(self.master_fd, "/new", append_return=True)
                    await asyncio.sleep(0.35)

                logger.info(
                    f"Injecting prompt via Bracketed Paste: '{prompt[:60]}...' "
                    f"(chars={len(prompt)})"
                )
                metrics = await self.jitter_simulator.paste_into_fd(
                    self.master_fd,
                    prompt,
                    append_return=True
                )
                self.last_jitter_metrics = metrics
            else:
                sanitized_prompt = prompt.replace("\r", " ").replace("\n", " ").strip()
                if not sanitized_prompt:
                    sanitized_prompt = "Hello"

                if reset_context:
                    command_text = f"/new {sanitized_prompt}"
                else:
                    command_text = sanitized_prompt

                logger.info(
                    f"Injecting prompt via Keystroke Jitter: '{command_text[:60]}...' "
                    f"(chars={len(command_text)}, mode={self.jitter_config.mode})"
                )

                # Section III.A: Inject input using Keystroke Jitter Simulator
                metrics = await self.jitter_simulator.type_into_fd(
                    self.master_fd,
                    command_text,
                    append_return=True
                )
                self.last_jitter_metrics = metrics

            logger.info(
                f"Input entry completed: typed={metrics.typed_chars}, pasted={metrics.pasted_chars} in {metrics.compose_ms:.1f}ms "
                f"(max CPS: {metrics.max_cps}, pre-enter: {metrics.pre_enter_ms:.1f}ms)"
            )

            # Check if takeover appeared after prompt submit
            for _ in range(15):
                await asyncio.sleep(0.1)
                text_lower = self._raw_buffer.lower()
                if b"take over" in text_lower or b"takeover" in text_lower:
                    logger.info("Takeover modal appeared after submit. Confirming...")
                    os.write(self.master_fd, b"\r")
                    break

            # Poll for turn completion by watching log.jsonl and chat-messages.json
            start_time = asyncio.get_event_loop().time()
            full_response = ""
            reasoning_response = ""
            turn_completed = False
            active_chat_dir = chat_dir_before
            emitted_stream_length = 0

            while (asyncio.get_event_loop().time() - start_time) < TURN_TIMEOUT_SECONDS:
                await asyncio.sleep(0.15)
                
                current_chat_dir = self._get_project_chat_dir()
                if current_chat_dir and current_chat_dir != active_chat_dir:
                    active_chat_dir = current_chat_dir
                    log_pos = 0

                if active_chat_dir:
                    log_file = os.path.join(active_chat_dir, "log.jsonl")
                    if os.path.exists(log_file):
                        try:
                            current_size = os.path.getsize(log_file)
                            if current_size < log_pos:
                                log_pos = 0
                            if current_size > log_pos:
                                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                                    f.seek(log_pos)
                                    new_lines = f.readlines()
                                    log_pos = f.tell()

                                for line in new_lines:
                                    line_str = line.strip()
                                    if not line_str:
                                        continue
                                    try:
                                        event = json.loads(line_str)
                                        lvl = event.get("level")
                                        data = event.get("data") or {}
                                        msg = event.get("msg", "")
                                        status = data.get("status") or event.get("status") or data.get("statusCode")
                                        err_code = data.get("error") or data.get("errorCode") or ""

                                        # Fast-Fail on 401 Auth Error
                                        if status == 401 or "authentication failed" in msg.lower() or "authentication error" in msg.lower() or "invalid api key" in msg.lower():
                                            raise FreebuffAuthError(f"Freebuff API authentication failed: {msg or 'Invalid API key or session expired'}. Run ./login.sh to refresh credentials.")

                                        # Fast-Fail on 429 Rate / Spend / Quota Limit
                                        if status == 429 or err_code in ["free_mode_rate_limited", "spend_limited", "session_limit_reached", "free_mode_capacity_deferred"]:
                                            desc = msg or err_code or "Rate limit exceeded"
                                            raise FreebuffRateLimitError(f"Freebuff rate limit or quota exceeded: {desc}", error_code=str(err_code))

                                        # Fast-Fail on 403 Permission / Network / Geoblock
                                        if status == 403 or err_code in ["free_mode_cli_required", "country_not_allowed", "anonymous_network", "residential_proxy", "ip_capped"]:
                                            desc = msg or err_code or "Access forbidden"
                                            raise FreebuffPermissionError(f"Freebuff access denied: {desc}", error_code=str(err_code))

                                        # Fast-Fail on 5xx Upstream Server Error
                                        if isinstance(status, int) and 500 <= status <= 599:
                                            raise FreebuffServerError(f"Freebuff upstream server error (HTTP {status}): {msg}", status_code=status)

                                        # Check streaming chunk
                                        if event.get("type") == "chunk":
                                            chunk_text = event.get("content", "")
                                            is_reasoning = event.get("isReasoning", False)
                                            if chunk_text and on_chunk:
                                                on_chunk(chunk_text, is_reasoning)
                                                emitted_stream_length += len(chunk_text)
                                        # Check turn completion
                                        if event.get("type") in ["turn_complete", "step_complete", "run_complete", "turn_end"]:
                                            turn_completed = True
                                            break
                                    except FreebuffException:
                                        raise
                                    except Exception:
                                        pass
                        except Exception as e:
                            logger.debug(f"Error reading log.jsonl: {e}")

                if turn_completed:
                    break

            if not turn_completed:
                if b"API key validation failed" in self._raw_buffer or b"authentication error" in self._raw_buffer:
                    raise FreebuffAuthError("Freebuff authentication failed during turn: token expired or invalid in Keychain. Run ./login.sh to re-authenticate.")
                if emitted_stream_length == 0:
                    raw_check = strip_ansi(bytes(self._raw_buffer))
                    if "API key validation failed" in raw_check or "authentication error" in raw_check:
                        raise FreebuffAuthError("Freebuff authentication failed during turn. Run ./login.sh to refresh credentials.")

            # Extract final response from chat-messages.json
            if active_chat_dir:
                msg_file = os.path.join(active_chat_dir, "chat-messages.json")
                for _ in range(10):
                    if os.path.exists(msg_file):
                        break
                    await asyncio.sleep(0.2)

                if os.path.exists(msg_file):
                    try:
                        with open(msg_file, "r", encoding="utf-8", errors="replace") as f:
                            msgs = json.load(f)
                            for msg in reversed(msgs):
                                if msg.get("role") == "assistant" or msg.get("type") == "assistant":
                                    full_response = msg.get("content") or msg.get("text") or ""
                                    reasoning_response = msg.get("reasoning_content") or msg.get("reasoning")
                                    break
                    except Exception as e:
                        logger.debug(f"Error parsing chat-messages.json: {e}")

            if not full_response:
                raw_text = strip_ansi(bytes(self._raw_buffer))
                full_response = raw_text[-1000:].strip() if raw_text else "Turn completed."

            logger.info(f"Turn finished successfully (response len={len(full_response)})")
            return {
                "content": full_response,
                "reasoning_content": reasoning_response if reasoning_response else None,
                "chat_dir": active_chat_dir,
                "jitter_metrics": metrics.to_dict() if metrics else None
            }

    async def stop(self):
        """Stops the worker and cleans up resources cleanly."""
        self.is_running = False
        if self._loop and self.master_fd is not None:
            self._loop.remove_reader(self.master_fd)
        if self.pid:
            try:
                # Terminate the process group to ensure zsh and child freebuff exit
                os.killpg(self.pid, 9)
            except OSError:
                try:
                    os.kill(self.pid, 9)
                except OSError:
                    pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        self.master_fd = None
        self.pid = None
        logger.info("Freebuff worker stopped.")
