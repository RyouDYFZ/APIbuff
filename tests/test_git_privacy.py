"""Offline Git privacy regressions; all repositories and remotes are synthetic."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.git_privacy import prepare_git_privacy_bin
from app.worker import FreebuffWorker


REAL_GIT = shutil.which("git")
PRIVATE_REMOTE = "https://github.com/private-test-org/internal-test-repo.git"


def isolated_git_env():
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="Privacy Test",
        GIT_AUTHOR_EMAIL="privacy-test@example.invalid",
        GIT_COMMITTER_NAME="Privacy Test",
        GIT_COMMITTER_EMAIL="privacy-test@example.invalid",
        LC_ALL="C",
    )
    return env


def run_git(executable, args, directory, env, **kwargs):
    return subprocess.run(
        [str(executable), *args], cwd=directory, env=env,
        capture_output=True, text=True, timeout=5, **kwargs,
    )


def initialize_repo(directory, env):
    directory.mkdir()
    run_git(REAL_GIT, ["init", "-q"], directory, env, check=True)
    run_git(REAL_GIT, ["remote", "add", "origin", PRIVATE_REMOTE], directory, env, check=True)
    (directory / "tracked.txt").write_text("before\n")
    run_git(REAL_GIT, ["add", "tracked.txt"], directory, env, check=True)
    run_git(REAL_GIT, ["commit", "-qm", "Initial fixture"], directory, env, check=True)


@unittest.skipUnless(REAL_GIT, "Git is required for the local repository regressions")
class GitPrivacyRepositoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.repo = self.directory / "private repo's checkout"
        self.env = isolated_git_env()
        initialize_repo(self.repo, self.env)
        self.shim_dir = prepare_git_privacy_bin(str(self.directory / "account's home"))
        self.env["PATH"] = self.shim_dir + os.pathsep + self.env.get("PATH", os.defpath)

    def guarded_git(self, *args, **kwargs):
        return run_git("git", args, self.repo, self.env, **kwargs)

    def test_origin_queries_have_no_stdout_and_never_disclose_remote(self):
        variants = (
            ["remote", "get-url", "origin"],
            ["-C", str(self.repo), "remote", "get-url", "origin"],
            ["-C" + str(self.repo), "remote", "get-url", "origin"],
            ["-c", "color.ui=false", "-C", str(self.repo), "remote", "get-url", "--all", "origin"],
            ["--git-dir", str(self.repo / ".git"), "remote", "get-url", "--push", "origin"],
            ["--git-dir=" + str(self.repo / ".git"), "--work-tree=" + str(self.repo), "remote", "get-url", "origin"],
            ["--no-pager", "remote", "-v", "get-url", "--push", "--all", "origin"],
            ["--exec-path=/unused/git/helpers", "remote", "get-url", "origin"],
        )
        for args in variants:
            with self.subTest(args=args):
                result = self.guarded_git(*args)
                self.assertEqual(result.returncode, 128)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "fatal: No such remote 'origin'\n")

    def test_status_diff_and_commit_use_real_git(self):
        tracked = self.repo / "tracked.txt"
        tracked.write_text("after\n")
        for args in (["status", "--porcelain"], ["diff", "--exit-code"]):
            with self.subTest(args=args):
                expected = run_git(REAL_GIT, args, self.repo, self.env)
                actual = self.guarded_git(*args)
                self.assertTrue(expected.stdout)
                self.assertEqual((actual.returncode, actual.stdout, actual.stderr),
                                 (expected.returncode, expected.stdout, expected.stderr))
        self.guarded_git("add", "tracked.txt", check=True)
        message = "Document remote get-url origin behavior"
        result = self.guarded_git("commit", "-qm", message)
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = run_git(REAL_GIT, ["log", "-1", "--format=%s"], self.repo, self.env, check=True)
        self.assertEqual(recorded.stdout.strip(), message)
        self.assertEqual(self.guarded_git("status", "--porcelain", check=True).stdout, "")

    def test_other_remote_reads_and_origin_configuration_are_unchanged(self):
        upstream = "https://github.com/public-test-org/public-test-repo.git"
        self.guarded_git("remote", "add", "upstream", upstream, check=True)
        self.assertEqual(self.guarded_git("remote", "get-url", "upstream", check=True).stdout.strip(), upstream)
        # The guard blocks the named probe; it does not rewrite repository data.
        self.assertEqual(self.guarded_git("config", "--get", "remote.origin.url", check=True).stdout.strip(), PRIVATE_REMOTE)


class GitPrivacyForwardingTests(unittest.TestCase):
    def test_forwarding_preserves_argv_streams_cwd_and_exit_through_duplicate_shims(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            shim_dir = Path(prepare_git_privacy_bin(str(directory / "account")))
            other_shim_dir = Path(prepare_git_privacy_bin(str(directory / "other account")))
            alias_dir = directory / "shim alias"
            alias_dir.mkdir()
            (alias_dir / "git").symlink_to(shim_dir / "git")
            real_dir = directory / "real git's bin"
            real_dir.mkdir()
            real = real_dir / "git"
            real_py = real_dir / "git_impl.py"
            real_py.write_text(
                "import json, os, sys\n"
                "print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read(), "
                "'cwd': os.getcwd(), 'marker': os.environ['PRIVACY_TEST_MARKER'], "
                "'path': os.environ['PATH']}))\n"
                "sys.stderr.write('forwarded stderr\\n')\n"
                "sys.exit(37)\n"
            )
            real.write_text(
                "#!/bin/sh\n"
                f'exec {shlex.quote(sys.executable)} {shlex.quote(str(real_py))} "$@"\n'
            )
            real.chmod(0o700)
            env = dict(os.environ, PATH=os.pathsep.join(map(str, [alias_dir, other_shim_dir, shim_dir, alias_dir, real_dir])),
                       PRIVACY_TEST_MARKER="unchanged")
            variants = (
                ["commit", "-m", "remote get-url origin", "--", "space file", "$(literal)", ""],
                ["diff", "--", "remote", "get-url", "origin"],
                ["-c", "alias.note=remote get-url origin", "status", "--porcelain"],
            )
            for args in variants:
                with self.subTest(args=args):
                    result = run_git("git", args, directory, env, input="stdin is preserved\n")
                    self.assertEqual(result.returncode, 37)
                    self.assertEqual(result.stderr, "forwarded stderr\n")
                    self.assertEqual(json.loads(result.stdout), {
                        "argv": args, "stdin": "stdin is preserved\n",
                        "cwd": str(directory.resolve()), "marker": "unchanged", "path": env["PATH"],
                    })

    def test_missing_real_git_does_not_recurse_and_probe_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            shim_dir = prepare_git_privacy_bin(temporary)
            env = dict(os.environ, PATH=shim_dir + os.pathsep + shim_dir)
            probe = run_git("git", ["remote", "get-url", "origin"], temporary, env)
            self.assertEqual((probe.returncode, probe.stdout, probe.stderr),
                             (128, "", "fatal: No such remote 'origin'\n"))
            status = run_git("git", ["status"], temporary, env)
            self.assertEqual(status.returncode, 127)
            self.assertEqual(status.stdout, "")
            self.assertIn("not found", status.stderr)


@unittest.skipUnless(REAL_GIT, "Git is required for the offline worker regression")
class GitPrivacyWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_blocks_origin_after_login_shell_resets_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            repo = directory / "worker's private repo"
            env = isolated_git_env()
            initialize_repo(repo, env)
            home = directory / "worker's account"
            result_file = directory / "result.json"
            fake_cli = directory / "fake CLI's executable"
            fake_py = directory / "fake_cli_impl.py"
            fake_py.write_text(textwrap.dedent(f"""\
                import json, os, pathlib, shutil, subprocess, sys, time
                cwd = sys.argv[sys.argv.index('--cwd') + 1]
                results = {{'git': shutil.which('git')}}
                for name, args in [('origin', ['remote', 'get-url', 'origin']), ('status', ['status', '--porcelain'])]:
                    result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, timeout=5)
                    results[name] = [result.returncode, result.stdout, result.stderr]
                pathlib.Path({str(result_file)!r}).write_text(json.dumps(results))
                print('Enter a coding task', flush=True)
                time.sleep(30)
                """))
            fake_cli.write_text(
                "#!/bin/sh\n"
                f'exec {shlex.quote(sys.executable)} {shlex.quote(str(fake_py))} "$@"\n'
            )
            fake_cli.chmod(0o700)
            login_shell = directory / "login shell's reset"
            login_shell.write_text(
                "#!/bin/sh\n"
                '[ "$1" = -l ] || exit 91\n'
                "shift\n"
                f"export PATH={shlex.quote(str(Path(REAL_GIT).parent) + ':/usr/bin:/bin')}\n"
                'exec /bin/sh "$@"\n'
            )
            login_shell.chmod(0o700)
            worker = FreebuffWorker(workdir=str(repo), home_dir=str(home))
            with patch.dict(os.environ, dict(env, FREEBUFF_HARDWARE_PRIVACY="off"), clear=True), \
                 patch("app.worker.verify_official_cli"), \
                 patch("app.worker.FREEBUFF_BIN", str(fake_cli)), \
                 patch("app.worker.SHELL_PATH", str(login_shell)), \
                 patch("app.worker.USE_LOGIN_SHELL", True), \
                 patch.object(worker, "_ensure_git_repo"):
                try:
                    await worker.start()
                    result = json.loads(result_file.read_text())
                    self.assertEqual(Path(result["git"]).resolve(), (home / "fake_bin" / "git").resolve())
                    self.assertEqual(result["origin"], [128, "", "fatal: No such remote 'origin'\n"])
                    self.assertEqual(result["status"], [0, "", ""])
                finally:
                    await worker.stop()


if __name__ == "__main__":
    unittest.main()
