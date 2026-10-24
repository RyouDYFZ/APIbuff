"""Local checks for macOS ps and Linux procfs supervisor process views."""

import asyncio
import errno
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.process_view import (
    SHIM_DIR, SHIM_DIR_KEY, SHIM_SETTING, SUPERVISOR_PID_KEY,
    configure_supervisor_process_name, supervisor_child_environment,
    supervisor_ps_shim_enabled, supervisor_terminal_view_enabled,
)
from app.terminal_env import audit_runtime_environment, bucket_process_name
from app.worker import FreebuffWorker, _terminal_geometry


class ProcessViewConfigurationTests(unittest.TestCase):
    def test_terminal_view_is_supported_on_linux_and_macos(self):
        for platform in ("linux", "darwin"):
            self.assertTrue(supervisor_terminal_view_enabled({}, platform=platform))
            self.assertFalse(supervisor_terminal_view_enabled({SHIM_SETTING: "false"}, platform=platform))
        self.assertFalse(supervisor_terminal_view_enabled({}, platform="win32"))

    def test_macos_default_and_explicit_disable(self):
        self.assertTrue(supervisor_ps_shim_enabled({}, platform="darwin"))
        for value in ("false", "0", "", "invalid"):
            self.assertFalse(supervisor_ps_shim_enabled({SHIM_SETTING: value}, platform="darwin"))
        for platform in ("linux", "win32"):
            self.assertFalse(supervisor_ps_shim_enabled({SHIM_SETTING: "true"}, platform=platform))

    def test_owned_pid_replaces_stale_values_without_mutating_caller(self):
        original = {"PATH": "/old/shim:/bin", SHIM_DIR_KEY: "/old/shim", SUPERVISOR_PID_KEY: "12"}
        result = supervisor_child_environment(original, supervisor_pid=345, platform="darwin")
        self.assertEqual(result[SUPERVISOR_PID_KEY], "345")
        self.assertEqual(result["PATH"], f"{SHIM_DIR}:/bin")
        self.assertEqual(original[SUPERVISOR_PID_KEY], "12")
        self.assertEqual(original["PATH"], "/old/shim:/bin")

    def test_disable_and_linux_clear_inherited_shim(self):
        for platform, setting in (("linux", "true"), ("darwin", "false")):
            original = {
                "PATH": f"{SHIM_DIR}:/old/shim:/usr/bin:/bin", SHIM_SETTING: setting,
                SHIM_DIR_KEY: "/old/shim", SUPERVISOR_PID_KEY: "12",
            }
            result = supervisor_child_environment(original, platform=platform)
            self.assertEqual(result["PATH"], "/usr/bin:/bin")
            self.assertNotIn(SHIM_DIR_KEY, result)
            self.assertNotIn(SUPERVISOR_PID_KEY, result)


class LinuxProcessNameTests(unittest.TestCase):
    def test_disabled_or_other_platform_does_not_load_native_library(self):
        with patch("app.process_view.ctypes.CDLL") as library:
            for platform, env in (("darwin", {}), ("win32", {}),
                                  ("linux", {SHIM_SETTING: "false"})):
                self.assertFalse(configure_supervisor_process_name(env, platform=platform))
            library.assert_not_called()

    def test_denied_name_change_stops_before_child_launch(self):
        from app.worker_supervisor import supervise
        with patch("app.process_view.sys.platform", "linux"), \
             patch.dict(os.environ, {SHIM_SETTING: "true"}), \
             patch("app.process_view.ctypes.CDLL") as library, \
             patch("app.process_view.ctypes.get_errno", return_value=errno.EPERM), \
             patch("app.worker_supervisor.subprocess.Popen") as spawn:
            library.return_value.prctl.return_value = -1
            with self.assertRaisesRegex(OSError, "Cannot set Linux supervisor process name"):
                supervise(-1, -1, ["unused"])
            spawn.assert_not_called()

    @unittest.skipUnless(sys.platform == "linux", "Requires Linux procfs and prctl")
    def test_name_change_is_scoped_to_current_process_and_preserves_identity(self):
        parent_name = Path("/proc/self/comm").read_text()
        source = textwrap.dedent("""
            import json, os, subprocess, sys
            from pathlib import Path
            from app.process_view import configure_supervisor_process_name
            before = [os.getpid(), os.getppid(), os.readlink('/proc/self/exe')]
            configure_supervisor_process_name({})
            after = [os.getpid(), os.getppid(), os.readlink('/proc/self/exe')]
            child = subprocess.check_output([sys.executable, '-c',
                "from pathlib import Path; print(Path('/proc/self/comm').read_text().strip())"], text=True)
            print(json.dumps({'before': before, 'after': after,
                'name': Path('/proc/self/comm').read_text().strip(), 'child_name': child.strip()}))
        """)
        result = subprocess.run([sys.executable, "-c", source], check=True, capture_output=True,
                                text=True, timeout=5, cwd=Path(__file__).resolve().parents[1])
        data = json.loads(result.stdout)
        self.assertEqual(data["before"], data["after"])
        self.assertEqual(data["name"], "terminal")
        self.assertEqual(bucket_process_name(data["child_name"]), "python")
        self.assertEqual(Path("/proc/self/comm").read_text(), parent_name)


@unittest.skipUnless(sys.platform == "darwin", "The ps view is only enabled on macOS")
class PsWrapperTests(unittest.TestCase):
    def setUp(self):
        self.env = supervisor_child_environment(os.environ, supervisor_pid=os.getpid())

    def query(self, args, *, system=False, env=None):
        return subprocess.run(
            ["/bin/ps" if system else str(SHIM_DIR / "ps"), *args],
            env=self.env if env is None else env, capture_output=True, timeout=2,
        )

    def test_only_owned_python_query_changes_comm_and_preserves_ppid(self):
        args = ["-o", "ppid=,comm=", "-p", str(os.getpid())]
        actual = self.query(args, system=True)
        viewed = self.query(args)
        real_parent, real_name = actual.stdout.decode().strip().split(None, 1)
        view_parent, view_name = viewed.stdout.decode().strip().split(None, 1)
        self.assertEqual(viewed.returncode, 0)
        self.assertEqual(view_parent, real_parent)
        self.assertEqual(bucket_process_name(real_name), "python")
        self.assertEqual(bucket_process_name(view_name), "terminal")

    def test_other_pids_orders_columns_and_extra_arguments_pass_through(self):
        pid = str(os.getpid())
        cases = (
            ["-o", "ppid=,comm=", "-p", str(os.getppid())],
            ["-p", pid, "-o", "ppid=,comm="],
            ["-o", "pid=,ppid=,comm=", "-p", pid],
            ["-o", "ppid=,comm=", "-p", pid, "-ww"],
            ["-o", "not-a-valid-column", "-p", pid],
        )
        for args in cases:
            with self.subTest(args=args):
                actual, viewed = self.query(args, system=True), self.query(args)
                self.assertEqual((viewed.returncode, viewed.stdout, viewed.stderr),
                                 (actual.returncode, actual.stdout, actual.stderr))

    def test_missing_pid_preserves_failure_instead_of_inventing_terminal(self):
        env = dict(self.env, **{SUPERVISOR_PID_KEY: "2147483647"})
        args = ["-o", "ppid=,comm=", "-p", env[SUPERVISOR_PID_KEY]]
        actual, viewed = self.query(args, system=True), self.query(args, env=env)
        self.assertNotEqual(actual.returncode, 0)
        self.assertEqual((viewed.returncode, viewed.stdout, viewed.stderr),
                         (actual.returncode, actual.stdout, actual.stderr))

    def test_missing_target_env_is_plain_passthrough(self):
        env = dict(self.env)
        env.pop(SUPERVISOR_PID_KEY)
        args = ["-o", "ppid=,comm=", "-p", str(os.getpid())]
        actual, viewed = self.query(args, system=True), self.query(args, env=env)
        self.assertEqual(viewed.stdout, actual.stdout)


def make_probe(directory, *, launcher=False):
    report = directory / "process-view.json"
    cli = directory / "probe cli's executable"
    cli_impl = directory / "probe_cli_impl.py"
    cli_impl.write_text(textwrap.dedent(f"""
        import json, os, shutil, subprocess, sys, time
        from pathlib import Path
        def read(pid, system=False):
            if sys.platform == 'linux' and not system:
                stat = Path('/proc/' + str(pid) + '/stat').read_text()
                start, end = stat.index('('), stat.rindex(')')
                fields = stat[end + 2:].split(' ')
                return int(fields[1]), stat[start + 1:end]
            value = subprocess.check_output([
                '/bin/ps' if system else 'ps', '-o', 'ppid=,comm=', '-p', str(pid)
            ], timeout=2).decode().strip().split(None, 1)
            return int(value[0]), value[1]
        parent = os.getppid()
        skipped = os.environ.get('CODEBUFF_LAUNCHER_PID') == str(parent)
        if skipped:
            parent = read(parent)[0]
        p = read(parent)
        g = read(p[0])
        actual_g = read(p[0], system=True)
        result = {{'pid': os.getpid(), 'parent': p[1], 'grandparent': g[1],
                  'actual_grandparent': actual_g[1], 'launcher': skipped,
                  'launcher_pid': os.environ.get('CODEBUFF_LAUNCHER_PID'),
                  'supervisor_pid': p[0], 'ps': shutil.which('ps'),
                  'lookup_source': 'proc' if sys.platform == 'linux' else 'ps',
                  'isatty': [os.isatty(fd) for fd in (0, 1, 2)],
                  'size': list(os.get_terminal_size(0))}}
        with open({str(report)!r}, 'w') as handle:
            json.dump(result, handle)
        print('Enter a coding task', flush=True)
        time.sleep(60)
    """))
    cli.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(cli_impl))} "$@"\n'
    )
    cli.chmod(0o700)
    if launcher:
        wrapper = directory / "probe launcher"
        wrapper_impl = directory / "probe_launcher_impl.py"
        wrapper_impl.write_text(textwrap.dedent(f"""
            import os, subprocess, sys
            os.environ['CODEBUFF_LAUNCHER_PID'] = str(os.getpid())
            raise SystemExit(subprocess.call([{str(cli)!r}, *sys.argv[1:]]))
        """))
        wrapper.write_text(
            "#!/bin/sh\n"
            f'exec {shlex.quote(sys.executable)} {shlex.quote(str(wrapper_impl))} "$@"\n'
        )
        wrapper.chmod(0o700)
        return wrapper, report
    return cli, report


@unittest.skipUnless(sys.platform in ("darwin", "linux"), "Requires a supported PTY platform")
class WorkerProcessViewTests(unittest.IsolatedAsyncioTestCase):
    async def inspect_worker(self, *, launcher=False, enabled=True):
        with tempfile.TemporaryDirectory(prefix="worker process view ") as temp:
            directory = Path(temp)
            cli, report = make_probe(directory, launcher=launcher)
            home = directory / "home"
            home.mkdir()
            # Deliberately overwrite PATH during login startup. The worker
            # must restore the shim after this file and preserve quoting.
            (home / ".zprofile").write_text("export PATH=/usr/bin:/bin\n")
            worker = FreebuffWorker(workdir=temp, home_dir=str(home))
            with patch("app.worker.FREEBUFF_BIN", str(cli)), \
                 patch("app.worker.verify_private_cli"), \
                 patch("app.worker.SHELL_PATH", "/bin/zsh"), \
                 patch("app.worker.USE_LOGIN_SHELL", True), \
                 patch.object(worker, "_ensure_git_repo"), \
                 patch.dict(os.environ, {
                     SHIM_SETTING: str(enabled).lower(),
                     # This harness is a Python fake CLI, not a Bun binary.
                     "FREEBUFF_HARDWARE_PRIVACY": "off",
                 }):
                try:
                    await asyncio.wait_for(worker.start(), 10)
                    data = json.loads(report.read_text())
                    self.assertEqual(data["supervisor_pid"], worker._supervisor.pid)
                    self.assertEqual(bucket_process_name(data["parent"]), "shell")
                    self.assertEqual(bucket_process_name(data["grandparent"]), "terminal" if enabled else "python")
                    system_kind = "terminal" if sys.platform == "linux" and enabled else "python"
                    self.assertEqual(bucket_process_name(data["actual_grandparent"]), system_kind)
                    self.assertEqual(data["launcher"], launcher)
                    self.assertEqual(data["isatty"], [True, True, True])
                    self.assertEqual(data["size"], list(_terminal_geometry(worker.account_id)))
                    if enabled and sys.platform == "darwin":
                        self.assertEqual(data["ps"], str(SHIM_DIR / "ps"))
                    if sys.platform == "linux":
                        self.assertEqual(data["lookup_source"], "proc")
                        self.assertNotEqual(data["ps"], str(SHIM_DIR / "ps"))
                    audit = await asyncio.to_thread(
                        audit_runtime_environment, data["pid"], launcher_pid=data["launcher_pid"],
                    )
                    self.assertEqual(audit["grandparent_kind"], system_kind)
                    if sys.platform == "linux":
                        host = next(item for item in audit["ancestry"] if item["pid"] == worker._supervisor.pid)
                        self.assertEqual(bucket_process_name(host["executable"]), "python")
                    self.assertFalse(audit["descriptor_verified"])
                finally:
                    await worker.stop()
                self.assertIsNone(worker._supervisor)

    async def test_direct_cli_after_login_path_reset(self):
        await self.inspect_worker()

    async def test_launcher_is_skipped_before_two_ancestor_queries(self):
        await self.inspect_worker(launcher=True)

    async def test_disabled_mode_keeps_original_python_view(self):
        await self.inspect_worker(enabled=False)


if __name__ == "__main__":
    unittest.main()
