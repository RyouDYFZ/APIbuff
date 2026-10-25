import os
import sys
import tempfile
import getpass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import patch
from app.workspace import (
    sanitize_host_paths,
    is_safe_isolated_path,
    resolve_target_workspace,
    get_workspace_git_info
)
from app.terminal_env import get_standard_terminal_env

class TestIsolationGuards(unittest.TestCase):
    def test_sanitize_host_paths(self):
        # 1. macOS personal path
        raw1 = "Fix the error in /Users/developer/Documents/MyProject/main.py immediately"
        scrubbed1 = sanitize_host_paths(raw1)
        self.assertNotIn("developer", scrubbed1)
        self.assertIn("/workspace/Documents/MyProject/main.py", scrubbed1)

        # 2. Linux personal home path
        raw2 = "Current Working Directory: /home/ubuntu/repo/app.py"
        scrubbed2 = sanitize_host_paths(raw2)
        self.assertNotIn("ubuntu", scrubbed2)
        self.assertIn("/workspace/repo/app.py", scrubbed2)

        # 3. Windows personal user path
        raw3 = "Path is C:\\Users\\Administrator\\Desktop\\file.txt"
        scrubbed3 = sanitize_host_paths(raw3)
        self.assertNotIn("Administrator", scrubbed3)

    def test_is_safe_isolated_path(self):
        with tempfile.TemporaryDirectory() as allowed_root, patch.dict(os.environ, {
            "FREEBUFF_WORKER_WORKDIR": allowed_root,
            "FREEBUFF_ALLOWED_WORKSPACE_ROOTS": "",
        }), patch("app.workspace.STRICT_ISOLATION", True):
            # Sensitive host personal paths must be blocked in strict isolation
            self.assertFalse(is_safe_isolated_path("/Users/developer/Documents"))
            self.assertFalse(is_safe_isolated_path("/Users/developer"))
            self.assertFalse(is_safe_isolated_path(str(Path.home())))
            self.assertFalse(is_safe_isolated_path("/private/var/folders/mc"))

            # Isolated sandbox paths must be allowed
            self.assertTrue(is_safe_isolated_path(allowed_root))

    def test_workspace_git_info_scrubbing(self):
        with patch("app.workspace.STRICT_ISOLATION", True):
            git_info = get_workspace_git_info(os.getcwd())
            # Remote URL must be suppressed in strict isolation
            self.assertIsNone(git_info.get("remote_url"), "Git remote URL must be redacted in strict isolation!")
            # Path must be sanitized and not leak host user identity
            current_user = getpass.getuser()
            if current_user:
                self.assertNotIn(current_user, git_info.get("path", ""))

    def test_terminal_env_scrubbing(self):
        env = get_standard_terminal_env()
        # Identity must be neutralized
        self.assertEqual(env["USER"], "developer")
        self.assertEqual(env["LOGNAME"], "developer")
        # macOS tracking variables must be removed
        for k in env:
            self.assertFalse(k.startswith("Apple_"), f"Found leaking Apple env: {k}")
            self.assertFalse(k.startswith("XPC_"), f"Found leaking XPC env: {k}")
        # PATH must not contain host user directories
        current_user = getpass.getuser()
        for p in env.get("PATH", "").split(":"):
            if current_user:
                self.assertNotIn(current_user, p, f"Found leaking user path in PATH: {p}")

    def test_gateway_secrets_never_reach_the_worker_environment(self):
        # Built by concatenation to keep credential-shaped literals out of the
        # source; these are env-var names carrying dummy probe values.
        secret_names = ("FREEBUFF" + "_API_KEY", "CODEBUFF" + "_API_KEY",
                        "AWS_SECRET" + "_ACCESS_KEY")
        host_hints = {"TZ": "Asia/Shanghai",
                      "SSH_CONNECTION": "10.0.0.1 1 10.0.0.2 2",
                      "ITERM_SESSION_ID": "w0t0p0:secret"}
        with patch.dict(os.environ, {**{name: "leak-probe" for name in secret_names},
                                     **host_hints}):
            env = get_standard_terminal_env()
        for name in secret_names:
            self.assertNotIn(name, env, f"{name} leaked into the worker environment")
        for key in host_hints:
            self.assertNotIn(key, env)
        for key in ("TERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "COLORTERM", "SHELL",
                    "HOME", "USER", "LOGNAME", "LANG", "COLUMNS", "LINES", "PATH",
                    "CODEBUFF_SHIP_LOGS"):
            self.assertIn(key, env)
        # The PostHog SDK ignores environment switches; telemetry is refused by
        # the gateway proxy, so no PostHog variable is ever injected.
        self.assertNotIn("CODEBUFF_DISABLE_POSTHOG", env)

    def test_inherited_or_overridden_cli_trust_cannot_bypass_gateway_opt_in(self):
        for value in ("1", "true", "yes"):
            with self.subTest(value=value), patch.dict(os.environ, {
                "FREEBUFF_TRUST_AGENTS": "false",
                "CODEBUFF_TRUST_AGENT_DIRS": value,
            }):
                self.assertNotIn("CODEBUFF_TRUST_AGENT_DIRS", get_standard_terminal_env())
                self.assertNotIn("CODEBUFF_TRUST_AGENT_DIRS", get_standard_terminal_env({
                    "CODEBUFF_TRUST_AGENT_DIRS": value,
                }))

if __name__ == "__main__":
    unittest.main()
