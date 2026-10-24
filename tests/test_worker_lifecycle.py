"""Offline lifecycle regression tests; never launches Freebuff or calls its API."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import selectors
import shlex
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lifecycle import gateway_control, stop_registered_gateways
from app.cli_build import AUDITED_BUNDLE_SHA256, PRIVATE_GUARD, sha256_file
from app.worker import FreebuffWorker
from app.worker_supervisor import supervise

PROJECT = Path(__file__).resolve().parent.parent


def process_is_live(pid):
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True
    )
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


def make_fake_cli(directory):
    # Exercise shell quoting as well as supervisor cleanup of TERM-resistant
    # descendants, which used to survive when the gateway was SIGKILLed.
    script = directory / "fake cli's executable"
    script_impl = directory / "fake_cli_impl.py"
    script_impl.write_text(
        "import json, os, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        f"child = subprocess.Popen([{sys.executable!r}, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.signal(signal.SIGHUP, signal.SIG_IGN); time.sleep(60)'])\n"
        f"open({str(directory / 'children.json')!r}, 'w').write(str(os.getpid()) + ',' + str(child.pid))\n"
        f"open({str(directory / 'environment.json')!r}, 'w').write(json.dumps({{'config': os.environ.get('FREEBUFF_CONFIG_DIR'), 'home': os.environ.get('HOME'), 'has_api_key': 'CODEBUFF_API_KEY' in os.environ, 'has_auth_token': 'FREEBUFF_AUTH_TOKEN' in os.environ, 'posthog_disabled': os.environ.get('CODEBUFF_DISABLE_POSTHOG'), 'ship_logs': os.environ.get('CODEBUFF_SHIP_LOGS')}}))\n"
        "print('Enter a coding task', flush=True)\n"
        "time.sleep(60)\n"
    )
    script.write_text(
        "#!/bin/sh\n"
        "# if (gatewayPostHogDisabled()) return gatewayDisabledPostHogClient();\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(script_impl))} "$@"\n'
    )
    script.chmod(0o700)
    # This offline fixture deliberately substitutes for a verified build.
    script.with_name(script.name + ".privacy.json").write_text(json.dumps({
        "privacy_build_version": 1,
        "platform": platform.system().lower(),
        "machine": platform.machine(),
        "posthog_switch": "CODEBUFF_DISABLE_POSTHOG",
        "source_bundle_sha256": AUDITED_BUNDLE_SHA256,
        "guard_sha256": sha256_file(PRIVATE_GUARD),
        "binary_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
    }))
    return script


@patch.dict(os.environ, {"FREEBUFF_HARDWARE_PRIVACY": "off"})
class WorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_reaps_supervisor_and_all_group_children(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            fake = make_fake_cli(directory)
            worker = FreebuffWorker(workdir=temp, home_dir=str(directory / "home"), token="test-account-token")
            with patch("app.worker.FREEBUFF_BIN", str(fake)), patch("app.worker.SHELL_PATH", "/bin/sh"), patch("app.worker.USE_LOGIN_SHELL", False), patch.object(worker, "_ensure_git_repo"):
                try:
                    with patch.dict(os.environ, {"FREEBUFF_CONFIG_DIR": str(directory / "wrong-config"), "CODEBUFF_API_KEY": "different-host-token", "FREEBUFF_AUTH_TOKEN": "different-host-token", "CODEBUFF_DISABLE_POSTHOG": "false", "CODEBUFF_SHIP_LOGS": "true"}):
                        await worker.start()
                    environment = json.loads((directory / "environment.json").read_text())
                    self.assertEqual(environment["config"], str(directory / "home" / ".config" / "manicode"))
                    self.assertEqual(environment["home"], str(directory / "home"))
                    self.assertFalse(environment["has_api_key"])
                    self.assertFalse(environment["has_auth_token"])
                    self.assertEqual(environment["posthog_disabled"], "1")
                    self.assertEqual(environment["ship_logs"], "false")
                    credentials = json.loads((Path(worker.config_dir) / "credentials.json").read_text())
                    self.assertEqual(credentials["default"]["authToken"], "test-account-token")
                    supervisor = worker._supervisor
                    children = [int(pid) for pid in (directory / "children.json").read_text().split(",")]
                    self.assertTrue(worker.is_running)
                    await worker.stop()
                    await worker.stop()  # Idempotent; no stale PID signals.
                    self.assertIsNotNone(supervisor.returncode)
                    self.assertFalse(worker.is_running)
                    self.assertIsNone(worker.master_fd)
                    self.assertTrue(all(not process_is_live(pid) for pid in children))
                finally:
                    await worker.stop()

    def test_chat_lookup_never_falls_back_to_host_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            host = directory / "host"
            account = directory / "account"
            host_chat = host / ".config" / "manicode" / "projects" / "workspace" / "chats" / "host-chat"
            host_chat.mkdir(parents=True)
            worker = FreebuffWorker(workdir=str(directory / "workspace"), home_dir=str(account))
            with patch.dict(os.environ, {"HOME": str(host)}):
                self.assertIsNone(worker._get_project_chat_dir())
            account_chat = Path(worker.config_dir) / "projects" / "workspace" / "chats" / "account-chat"
            account_chat.mkdir(parents=True)
            self.assertEqual(worker._get_project_chat_dir(), str(account_chat))

    async def test_startup_exit_fails_and_closes_terminal(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = FreebuffWorker(workdir=temp, home_dir=str(Path(temp) / "home"))
            with patch("app.worker.verify_private_cli"), patch("app.worker.FREEBUFF_BIN", "/nonexistent/freebuff-test"), patch("app.worker.SHELL_PATH", "/bin/sh"), patch("app.worker.USE_LOGIN_SHELL", False), patch.object(worker, "_ensure_git_repo"):
                with self.assertRaisesRegex(RuntimeError, "exited during startup"):
                    await worker.start()
            self.assertFalse(worker.is_running)
            self.assertIsNone(worker.master_fd)
            self.assertIsNone(worker._supervisor)

    async def test_control_socket_stops_only_its_registered_gateway(self):
        with tempfile.TemporaryDirectory() as temp:
            stopped = asyncio.Event()
            control = Path(temp) / "control"
            async def gateway():
                async with gateway_control(control, on_stop=stopped.set):
                    await stopped.wait()
            task = asyncio.create_task(gateway())
            for _ in range(50):
                if list(control.glob("*.sock")):
                    break
                await asyncio.sleep(0.01)
            # Signal is handled by the live socket owner, never a stored PID.
            result = await asyncio.to_thread(stop_registered_gateways, control, 2)
            await asyncio.wait_for(task, 2)
            self.assertEqual(result, 0)
            self.assertTrue(stopped.is_set())
            self.assertEqual(list(control.glob("*.sock")), [])


    async def test_other_project_control_socket_is_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            first, second = asyncio.Event(), asyncio.Event()
            async def gateway(path, event):
                async with gateway_control(path, on_stop=event.set):
                    await event.wait()
            first_dir, second_dir = Path(temp) / "first", Path(temp) / "second"
            first_task = asyncio.create_task(gateway(first_dir, first))
            second_task = asyncio.create_task(gateway(second_dir, second))
            try:
                for _ in range(50):
                    if list(first_dir.glob("*.sock")) and list(second_dir.glob("*.sock")):
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(await asyncio.to_thread(stop_registered_gateways, first_dir, 2), 0)
                await first_task
                self.assertFalse(second.is_set())
                self.assertEqual(len(list(second_dir.glob("*.sock"))), 1)
            finally:
                first.set()
                second.set()
                await asyncio.gather(first_task, second_task)


@patch.dict(os.environ, {"FREEBUFF_HARDWARE_PRIVACY": "off"})
class AbruptExitTests(unittest.TestCase):
    def test_handshake_broken_pipe_still_cleans_child(self):
        read_fd, write_fd = os.pipe()
        terminal_fd = os.open(os.devnull, os.O_RDWR)
        try:
            with patch("app.worker_supervisor.configure_supervisor_process_name"), patch("app.worker_supervisor.subprocess.Popen") as launch, patch("app.worker_supervisor.signal.signal"), patch("app.worker_supervisor._signal_group", return_value=False) as group_signal, patch("builtins.print", side_effect=BrokenPipeError):
                child = launch.return_value
                child.pid = 123456
                with self.assertRaises(BrokenPipeError):
                    supervise(read_fd, terminal_fd, ["fake"])
                child.wait.assert_called_once()
                self.assertIn(unittest.mock.call(child.pid, signal.SIGTERM), group_signal.call_args_list)
                self.assertIn(unittest.mock.call(child.pid, signal.SIGKILL), group_signal.call_args_list)
        finally:
            os.close(write_fd)
    def test_gateway_sigkill_closes_worker_lifetime_pipe(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            fake = make_fake_cli(directory)
            harness = textwrap.dedent(f"""
                import asyncio, json
                import app.worker as module
                module.FREEBUFF_BIN = {str(fake)!r}
                module.SHELL_PATH = '/bin/sh'
                module.USE_LOGIN_SHELL = False
                module.FreebuffWorker._ensure_git_repo = lambda self: None
                async def run():
                    worker = module.FreebuffWorker(workdir={temp!r}, home_dir={str(directory / 'home')!r})
                    await worker.start()
                    print(json.dumps({{'pid': worker.pid, 'supervisor': worker._supervisor.pid}}), flush=True)
                    await asyncio.sleep(60)
                asyncio.run(run())
            """)
            parent = subprocess.Popen(
                [sys.executable, "-c", harness], cwd=PROJECT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(parent.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(timeout=10), "fake gateway did not boot")
                line = parent.stdout.readline()
                self.assertTrue(line, "fake gateway exited before ready")
                info = json.loads(line)
                children = [int(pid) for pid in (directory / "children.json").read_text().split(",")]
                parent.kill()
                parent.wait(timeout=5)
                pids = [info["supervisor"], info["pid"], *children]
                deadline = time.monotonic() + 6
                while any(process_is_live(pid) for pid in pids) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(all(not process_is_live(pid) for pid in pids), f"live child remains: {pids}")
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
                parent.stdout.close()
                parent.stderr.close()

    def test_stop_modes_do_not_cross_native_and_docker(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            calls = directory / "calls"
            for tool in ("docker", "python3"):
                script = directory / tool
                script.write_text(f'#!/bin/sh\necho "{tool} $*" >> "$LIFECYCLE_TEST_LOG"\n')
                script.chmod(0o700)
            env = dict(os.environ, PATH=f"{temp}:/usr/bin:/bin", LIFECYCLE_TEST_LOG=str(calls))
            native = subprocess.run(["/bin/bash", str(PROJECT / "stop.sh"), "--native"], env=env, capture_output=True, text=True)
            self.assertEqual(native.returncode, 0, native.stderr)
            self.assertEqual(calls.read_text().strip(), "python3 -m app.lifecycle")
            calls.write_text("")
            docker = subprocess.run(["/bin/bash", str(PROJECT / "stop.sh"), "--docker"], env=env, capture_output=True, text=True)
            self.assertEqual(docker.returncode, 0, docker.stderr)
            self.assertEqual(calls.read_text().splitlines(), ["docker compose version", "docker compose stop --timeout 15"])


if __name__ == "__main__":
    unittest.main()
