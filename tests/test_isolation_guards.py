import os
import sys

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
        raw1 = "Fix the error in /Users/dannyfeng/Documents/MyProject/main.py immediately"
        scrubbed1 = sanitize_host_paths(raw1)
        self.assertNotIn("dannyfeng", scrubbed1)
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
        with patch("app.workspace.STRICT_ISOLATION", True):
            # Sensitive host personal paths must be blocked in strict isolation
            self.assertFalse(is_safe_isolated_path("/Users/dannyfeng/Documents"))
            self.assertFalse(is_safe_isolated_path("/Users/dannyfeng"))
            self.assertFalse(is_safe_isolated_path("/private/var/folders/mc"))

            # Isolated sandbox paths must be allowed
            os.makedirs("/tmp/freebuff-api-worker", exist_ok=True)
            self.assertTrue(is_safe_isolated_path("/tmp/freebuff-api-worker"))

    def test_workspace_git_info_scrubbing(self):
        with patch("app.workspace.STRICT_ISOLATION", True):
            git_info = get_workspace_git_info(os.getcwd())
            # Remote URL must be suppressed in strict isolation
            self.assertIsNone(git_info.get("remote_url"), "Git remote URL must be redacted in strict isolation!")
            # Path must be sanitized
            self.assertNotIn("dannyfeng", git_info.get("path", ""))

    def test_terminal_env_scrubbing(self):
        env = get_standard_terminal_env()
        # Identity must be neutralized
        self.assertEqual(env["USER"], "developer")
        self.assertEqual(env["LOGNAME"], "developer")
        # macOS tracking variables must be removed
        for k in env:
            self.assertFalse(k.startswith("Apple_"), f"Found leaking Apple env: {k}")
            self.assertFalse(k.startswith("XPC_"), f"Found leaking XPC env: {k}")
        # PATH must not contain /Users/dannyfeng
        for p in env.get("PATH", "").split(":"):
            self.assertNotIn("dannyfeng", p, f"Found leaking user path in PATH: {p}")

if __name__ == "__main__":
    unittest.main()
