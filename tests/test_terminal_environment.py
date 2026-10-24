"""Offline regressions for process ancestry and descriptor predictions."""

from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.terminal_env import (
    audit_runtime_environment,
    bucket_process_name,
    compute_simulated_x_freebuff_env,
)


def descriptor_fields(descriptor):
    version, *fields = descriptor.split(";")
    assert version == "v1"
    return dict(field.split("=", 1) for field in fields)


class ProcessClassificationTests(unittest.TestCase):
    def test_process_names_use_fixed_categories_without_substring_guesses(self):
        cases = {
            "/opt/homebrew/bin/python3": "python",
            "/opt/homebrew/bin/python3.14": "python",
            "PyPy3.exe": "python",
            "python-helper": "other",
            "ipython": "other",
            "-zsh": "shell",
            "/bin/--bash": "shell",
            "pwsh.exe": "shell",
            "tmux: server": "mux",
            "zellij": "mux",
            "sshd-session": "sshd",
            "nodejs": "node",
            "bun": "bun",
            "/Applications/Terminal.app/Contents/MacOS/Terminal": "terminal",
            "stable": "terminal",
            "gnome-terminal-": "terminal",
            "terminal-helper": "other",
            "Ghostty": "terminal",
            "Code Helper (Plugin)": "editor",
            "electron helper": "editor",
            "nvim": "editor",
            "launchd": "other",
            "": "unknown",
            "   ": "unknown",
            None: "unknown",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(bucket_process_name(raw), expected)


class DescriptorPredictionTests(unittest.TestCase):
    def test_defaults_do_not_claim_terminal_ancestry_launcher_or_osc_reply(self):
        fields = descriptor_fields(compute_simulated_x_freebuff_env({}))
        self.assertEqual({key: fields[key] for key in ("p", "g", "l", "osc")}, {
            "p": "unknown", "g": "unknown", "l": "0", "osc": "na",
        })
        self.assertEqual(fields["sz"], "0x0")
        self.assertEqual(fields["tp"], "none")

    def test_configured_flags_and_proxy_do_not_leak_raw_values(self):
        fields = descriptor_fields(compute_simulated_x_freebuff_env({
            "TERM_PROGRAM": " WarpTerminal ", "TERM": "xterm-256color",
            "COLORTERM": "truecolor", "COLUMNS": "120.9", "LINES": "10000",
            "CI": "false", "GITHUB_ACTIONS": "true",
            "SSH_CONNECTION": "private-address port destination port",
            "HTTPS_PROXY": "https://secret:password@proxy.example:8443",
            "all_proxy": "socks5://[::1]:1080",
            "NODE_TLS_REJECT_UNAUTHORIZED": " 0 ",
            "NODE_EXTRA_CA_CERTS": "/private/certificate.pem", "TZ": "Asia/Shanghai",
            "CODESPACES": " TRUE ", "GITPOD_WORKSPACE_ID": "private-workspace",
        }, "shell", "python", launcher=True))
        self.assertEqual(fields, {
            "in": "1", "out": "1", "tp": "warp", "term": "1", "ct": "1",
            "sz": "120x9999", "ci": "1", "ssh": "1", "l": "1", "p": "shell",
            "g": "python", "osc": "na", "tzo": "1", "px": "loopback", "tls": "0",
            "ca": "1", "cde": "codespaces",
        })

    def test_false_ci_and_ssh_client_alone_are_not_positive_flags(self):
        fields = descriptor_fields(compute_simulated_x_freebuff_env({
            "CI": "false", "GITHUB_ACTIONS": "1", "SSH_CLIENT": "present",
            "NODE_EXTRA_CA_CERTS": "  ", "TZ": "  ",
            "COLUMNS": "NaN", "LINES": "-30", "TERM_PROGRAM": "myterminal",
        }))
        self.assertEqual(fields["ci"], "0")
        self.assertEqual(fields["ssh"], "0")
        self.assertEqual(fields["ca"], "0")
        self.assertEqual(fields["tzo"], "0")
        self.assertEqual(fields["sz"], "0x0")
        self.assertEqual(fields["tp"], "other")

    def test_system_zone_is_separate_from_tz_override(self):
        self.assertNotIn("stz", descriptor_fields(compute_simulated_x_freebuff_env({"TZ": "Asia/Shanghai"})))
        self.assertNotIn("stz", descriptor_fields(compute_simulated_x_freebuff_env({}, system_zone="Europe/Paris")))
        fields = descriptor_fields(compute_simulated_x_freebuff_env(
            {"TZ": "Asia/Shanghai"}, system_zone="/var/db/timezone/zoneinfo/Europe/Paris",
        ))
        self.assertEqual(fields["stz"], "Europe/Paris")

    def test_supported_terminal_programs_and_cloud_ide_priority(self):
        for program, bucket in (("iTerm.app", "iterm"), ("iterm2", "iterm"),
                                ("kitty", "kitty"), ("wezterm", "wezterm"),
                                ("jetbrains-jediterm", "jetbrains"), ("rio", "rio")):
            with self.subTest(program=program):
                fields = descriptor_fields(compute_simulated_x_freebuff_env({"TERM_PROGRAM": program}))
                self.assertEqual(fields["tp"], bucket)
        cases = (
            ({"GITPOD_WORKSPACE_ID": "id", "CLOUD_SHELL": "true"}, "gitpod"),
            ({"CLOUD_SHELL": "true", "CODER": "true"}, "cloudshell"),
            ({"CODER": "true"}, "coder"),
            ({"CODER": "false"}, "none"),
        )
        for env, expected in cases:
            with self.subTest(env=env):
                self.assertEqual(descriptor_fields(compute_simulated_x_freebuff_env(env))["cde"], expected)


class RuntimeAncestryTests(unittest.TestCase):
    def audit(self, processes, **kwargs):
        def read_ps(args, **options):
            self.assertEqual(args[:4], ["/bin/ps", "-o", "pid=,ppid=,comm=", "-p"])
            self.assertEqual(options["timeout"], 2)
            pid = int(args[-1])
            if pid not in processes:
                raise subprocess.CalledProcessError(1, args)
            parent, command = processes[pid]
            return f"{pid} {parent} {command}\n".encode()

        with patch("app.terminal_env.sys.platform", "darwin"), \
             patch("app.terminal_env.subprocess.check_output", side_effect=read_ps):
            return audit_runtime_environment(300, descriptor_env={"COLUMNS": "120", "LINES": "40"}, **kwargs)

    def test_direct_cli_reports_supervisor_as_python(self):
        result = self.audit({
            300: (200, "/path/freebuff"), 200: (100, "-zsh"),
            100: (1, "/opt/homebrew/bin/python3"), 1: (0, "launchd"),
        })
        self.assertEqual((result["parent_kind"], result["grandparent_kind"]), ("shell", "python"))
        self.assertFalse(result["launcher_skipped"])
        self.assertFalse(result["descriptor_verified"])
        self.assertEqual(result["ancestry_source"], "system_process_snapshot")
        self.assertFalse(result["interactive_shell_verified"])
        self.assertEqual(result["predicted_fields"], ["in", "out", "sz"])
        self.assertEqual(result["unobserved_fields"], ["osc"])

    def test_confirmed_direct_parent_launcher_is_skipped_once(self):
        processes = {
            300: (250, "/path/freebuff"), 250: (200, "node"),
            200: (100, "-zsh"), 100: (1, "/opt/homebrew/bin/python3"),
        }
        result = self.audit(processes, launcher_pid="250")
        self.assertEqual((result["parent_kind"], result["grandparent_kind"]), ("shell", "python"))
        self.assertTrue(result["launcher_skipped"])
        self.assertEqual(descriptor_fields(result["simulated_x_freebuff_env"])["l"], "1")
        # A node parent is insufficient evidence, and a stale PID must not skip it.
        for kwargs in ({}, {"launcher_pid": 200}):
            with self.subTest(kwargs=kwargs):
                unconfirmed = self.audit(processes, **kwargs)
                self.assertEqual((unconfirmed["parent_kind"], unconfirmed["grandparent_kind"]), ("node", "shell"))
                self.assertFalse(unconfirmed["launcher_skipped"])

    def test_missing_parent_is_unknown_even_with_known_launcher(self):
        result = self.audit({300: (250, "/path/freebuff")}, launcher_pid=250)
        self.assertTrue(result["launcher_skipped"])
        self.assertEqual((result["parent_kind"], result["grandparent_kind"]), ("unknown", "unknown"))

    def test_launcher_environment_does_not_implicitly_skip_processes(self):
        with patch("app.terminal_env.sys.platform", "darwin"), patch(
            "app.terminal_env.subprocess.check_output",
            side_effect=[b"300 250 freebuff\n", b"250 200 node\n", b"200 100 zsh\n", b"100 1 python3\n"],
        ):
            result = audit_runtime_environment(300, descriptor_env={"CODEBUFF_LAUNCHER_PID": "250"})
        self.assertFalse(result["launcher_skipped"])
        self.assertEqual(result["parent_kind"], "node")

    def test_init_process_can_be_observed_and_invalid_pid_is_not_queried(self):
        result = self.audit({300: (200, "freebuff"), 200: (1, "sh"), 1: (0, "launchd")})
        self.assertEqual(result["grandparent_kind"], "other")
        with patch("app.terminal_env.subprocess.check_output") as read_ps:
            invalid = audit_runtime_environment(-1, descriptor_env={})
        read_ps.assert_not_called()
        self.assertEqual(invalid["parent_kind"], "unknown")

    def test_windows_ancestry_is_not_looked_up(self):
        with patch("app.terminal_env.sys.platform", "win32"), patch("app.terminal_env.subprocess.check_output") as read_ps:
            result = audit_runtime_environment(300, descriptor_env={})
        read_ps.assert_not_called()
        self.assertEqual((result["parent_kind"], result["grandparent_kind"]), ("na", "na"))


if __name__ == "__main__":
    unittest.main()
