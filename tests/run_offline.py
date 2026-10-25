"""Run only local regressions; legacy endpoint scripts require a live gateway."""

from pathlib import Path
import sys
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

PATTERNS = (
    "test_hardware_privacy.py",
    "test_cli_build.py",
    "test_credentials.py",
    "test_container_credentials.py",
    "test_worker_lifecycle.py",
    "test_process_view.py",
    "test_terminal_environment.py",
    "test_input_and_log_stream.py",
    "test_terminal_colors.py",
    "test_response_attachments.py",
    "test_isolation_guards.py",
    "test_telemetry_blocklist.py",
    "test_git_privacy.py",
    "test_multi_account_pool.py",
    "test_catalog_truth.py",
    "test_chat_protocol.py",
    "test_diagnostics.py",
    "test_pool_sessions.py",
    "test_worker_turns.py",
    "test_transcript_polling.py",
    "test_workspace_proxy.py",
    "test_proxy_endpoint.py",
    "test_admission_and_fatal_modals.py",
    "test_proxy_loopback_and_ulimit.py",
)


if __name__ == "__main__":
    suite = unittest.TestSuite()
    for pattern in PATTERNS:
        suite.addTests(unittest.defaultTestLoader.discover(str(PROJECT / "tests"), pattern=pattern))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
