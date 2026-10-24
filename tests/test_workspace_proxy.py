"""Offline path-boundary, required-proxy, and maintenance-route regressions."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.accounts import AccountConfig, AccountManager, AccountState
from app.exceptions import FreebuffException, FreebuffPermissionError
from app.proxy_manager import ProxyManager, ProxyNode, redact_proxy_url, validate_proxy_url
from app import workspace


class TestWorkspaceBoundaries(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.allowed = self.root / "worker"
        self.allowed.mkdir()
        self.repo = self.allowed / "repo"
        self.repo.mkdir()
        self.outside = self.root / "host-sandbox-isolated-worker"
        self.outside.mkdir()
        env = patch.dict(os.environ, {
            "FREEBUFF_WORKER_WORKDIR": str(self.allowed),
            "FREEBUFF_ALLOWED_WORKSPACE_ROOTS": "",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_real_directory_boundary_applies_with_metadata_redaction_on_or_off(self):
        sibling = self.root / "worker-other"
        sibling.mkdir()
        for strict in (True, False):
            with self.subTest(strict=strict), patch.object(workspace, "STRICT_ISOLATION", strict):
                self.assertTrue(workspace.is_safe_isolated_path(str(self.repo)))
                self.assertFalse(workspace.is_safe_isolated_path(str(sibling)))
                self.assertFalse(workspace.is_safe_isolated_path(str(self.outside)))

    def test_symlink_inside_root_cannot_escape_to_host(self):
        link = self.allowed / "linked-repo"
        link.symlink_to(self.outside, target_is_directory=True)
        self.assertFalse(workspace.is_safe_isolated_path(str(link)))
        with self.assertRaises(FreebuffPermissionError):
            workspace.resolve_target_workspace(body_workspace=str(link))

    def test_traversal_cannot_escape_allowed_root(self):
        with self.assertRaises(FreebuffPermissionError):
            workspace.validate_workspace_path(str(self.allowed / ".." / self.outside.name))

    def test_extra_roots_and_normalization_are_explicit(self):
        second = self.root / "second"
        second.mkdir()
        with patch.dict(os.environ, {
            "FREEBUFF_ALLOWED_WORKSPACE_ROOTS": os.pathsep.join((str(self.outside), str(second))),
        }):
            self.assertEqual(workspace.validate_workspace_path(str(self.outside)), str(self.outside.resolve()))
            self.assertTrue(workspace.is_safe_isolated_path(str(second)))
        alias = self.root / "worker-alias"
        alias.symlink_to(self.allowed, target_is_directory=True)
        self.assertEqual(workspace.validate_workspace_path(str(alias / "repo")), str(self.repo.resolve()))

    def test_no_selection_leaves_per_account_default_to_pool(self):
        absent = self.root / "not-created"
        with patch.dict(os.environ, {"FREEBUFF_WORKER_WORKDIR": str(absent)}):
            self.assertIsNone(workspace.resolve_target_workspace())
        self.assertFalse(absent.exists())

    def test_invalid_explicit_header_does_not_fall_back_to_valid_body(self):
        for value, expected in (("", 400), ("  ", 400), (str(self.root / "missing"), 400), (str(self.outside), 403)):
            with self.subTest(value=value):
                request = SimpleNamespace(headers={"x-workspace-path": value})
                with self.assertRaises(FreebuffException) as caught:
                    workspace.resolve_target_workspace(http_request=request, body_workspace=str(self.repo))
                self.assertEqual(caught.exception.status_code, expected)

    def test_invalid_body_and_fallback_are_typed_errors(self):
        for value in ("", "  ", "\0invalid", str(self.root / "missing")):
            with self.subTest(value=value), self.assertRaises(FreebuffException) as caught:
                workspace.resolve_target_workspace(body_workspace=value)
            self.assertEqual(caught.exception.error_code, "invalid_workspace")
        with self.assertRaises(FreebuffPermissionError):
            workspace.resolve_target_workspace(default_fallback=str(self.outside))

    def test_user_or_assistant_text_cannot_select_a_workspace(self):
        for role in ("user", "assistant"):
            with self.subTest(role=role):
                message = SimpleNamespace(role=role, content=f"Current Working Directory: {self.repo}")
                self.assertIsNone(workspace.resolve_target_workspace(messages=[message]))

    def test_system_and_developer_paths_use_the_same_boundary(self):
        for role in ("system", "developer"):
            with self.subTest(role=role):
                message = SimpleNamespace(role=role, content=f"Current Working Directory: {self.repo}")
                self.assertEqual(workspace.resolve_target_workspace(messages=[message]), str(self.repo.resolve()))
                message.content = f"Current Working Directory: {self.outside}"
                with self.assertRaises(FreebuffPermissionError):
                    workspace.resolve_target_workspace(messages=[message])

    def test_every_git_subprocess_is_bounded(self):
        (self.repo / ".git").mkdir()
        with patch.object(workspace, "STRICT_ISOLATION", False), patch.object(
            workspace.subprocess, "check_output", side_effect=subprocess.TimeoutExpired("git", 5),
        ) as command:
            info = workspace.get_workspace_git_info(str(self.repo))
        self.assertTrue(info["is_git"])
        self.assertEqual(command.call_count, 5)
        self.assertTrue(all(call.kwargs["timeout"] == 5 for call in command.call_args_list))


class TestRequiredAccountProxies(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_file = self.root / "accounts.json"
        sandbox = patch("app.accounts.BASE_SANDBOX_DIR", str(self.root / "accounts"))
        sandbox.start()
        self.addCleanup(sandbox.stop)
        self.proxy = ProxyManager(singbox_bin=str(self.root / "no-binary"))
        proxy = patch("app.accounts.global_proxy_manager", self.proxy)
        proxy.start()
        self.addCleanup(proxy.stop)
        self.manager = AccountManager(str(self.config_file))
        seed = patch.object(self.manager, "_seed_sandbox")
        self.seed = seed.start()
        self.addCleanup(seed.stop)
        self.node = ProxyNode("chosen-node", "socks", "proxy.invalid", 1080, {"type": "socks"})

    def write_config(self, accounts, subscription=None):
        config = {"accounts": [{"id": "one", "name": "One", "token": "private-token", **account} for account in accounts]}
        if subscription is not None:
            config["subscription_url"] = subscription
        self.config_file.write_text(json.dumps(config))

    def fetch_one(self, _url):
        self.proxy.set_nodes([self.node])
        return self.proxy.nodes

    def test_failed_subscription_prevents_required_proxy_accounts_from_being_healthy(self):
        self.write_config([{}], "https://subscription.invalid/private-token")
        self.proxy.set_nodes([self.node])
        with patch.object(self.proxy, "fetch_subscription", return_value=[]), patch.object(self.proxy, "start_singbox") as start:
            account = self.manager.load_accounts()[0]
        self.assertEqual(account.status, "proxy_error")
        self.assertIsNone(account.assigned_proxy)
        self.assertEqual(self.manager.get_healthy_accounts(), [])
        self.assertEqual(self.proxy.nodes, [])
        start.assert_not_called()

    def test_filter_without_subscription_never_uses_old_nodes(self):
        self.write_config([{"node_filter": "chosen"}])
        self.proxy.set_nodes([self.node])
        account = self.manager.load_accounts()[0]
        self.assertEqual(account.status, "proxy_error")
        self.assertEqual(self.proxy.nodes, [])

    def test_unmatched_or_invalid_filter_does_not_take_first_node(self):
        for pattern in ("unmatched", "[", ""):
            with self.subTest(pattern=pattern):
                self.write_config([{"node_filter": pattern}], "https://subscription.invalid")
                with patch.object(self.proxy, "fetch_subscription", side_effect=self.fetch_one), patch.object(self.proxy, "start_singbox") as start:
                    account = self.manager.load_accounts()[0]
                self.assertEqual(account.status, "proxy_error")
                self.assertIsNone(account.assigned_proxy)
                start.assert_not_called()

    def test_missing_or_failed_daemon_blocks_every_bound_account(self):
        self.write_config([{}, {"id": "two"}], "https://subscription.invalid")
        with patch.object(self.proxy, "fetch_subscription", side_effect=self.fetch_one):
            accounts = self.manager.load_accounts()
        self.assertTrue(all(account.status == "proxy_error" for account in accounts))
        self.assertTrue(all(account.assigned_proxy is None for account in accounts))
        self.assertEqual(self.manager.get_healthy_accounts(), [])

    def test_successful_daemon_is_required_before_accounts_become_healthy(self):
        self.write_config([{}, {"id": "two"}], "https://subscription.invalid")
        with patch.object(self.proxy, "fetch_subscription", side_effect=self.fetch_one), patch.object(self.proxy, "start_singbox", return_value=True):
            accounts = self.manager.load_accounts()
        self.assertEqual(len(self.manager.get_healthy_accounts()), 2)
        self.assertNotEqual(accounts[0].assigned_proxy, accounts[1].assigned_proxy)

    def test_explicit_proxy_is_usable_even_when_unrelated_subscription_fails(self):
        endpoint = "http://private-user:private-password@127.0.0.1:1080"
        self.write_config([{"proxy": endpoint}, {"id": "disabled", "enabled": False, "token": ""}], "https://subscription.invalid")
        with patch.object(self.proxy, "fetch_subscription", return_value=[]):
            accounts = self.manager.load_accounts()
        self.assertEqual(accounts[0].status, "idle")
        self.assertEqual(accounts[0].assigned_proxy, endpoint)
        self.assertEqual(accounts[0].to_dict()["assigned_proxy"], "http://127.0.0.1:1080")
        self.assertEqual(accounts[1].status, "disabled")
        self.seed.assert_called_once_with(accounts[0])

    def test_explicit_invalid_proxy_rejects_config_before_seeding(self):
        self.write_config([{"proxy": "file:///private-token"}])
        with self.assertRaises(ValueError) as caught:
            self.manager.load_accounts()
        self.assertNotIn("private-token", str(caught.exception))
        self.seed.assert_not_called()

    def test_health_uses_positive_states_and_expired_cooldown_only(self):
        account = AccountState(AccountConfig(id="one", name="One", token="test-token"))
        self.manager.accounts = {"one": account}
        for state in ("auth_error", "proxy_error", "disabled", "unhealthy", "error", "starting", "stopped"):
            account.status = state
            with self.subTest(state=state):
                self.assertEqual(self.manager.get_healthy_accounts(), [])
        for state in ("idle", "busy", "cooling"):
            account.status = state
            with self.subTest(state=state):
                self.assertEqual(self.manager.get_healthy_accounts(), [account])
        account.cooldown_until = float("inf")
        self.assertEqual(self.manager.get_healthy_accounts(), [])


class TestProxyManagerLifecycle(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.binary = self.root / "fake-sing-box"
        self.binary.write_text("never executed")
        self.binary.chmod(0o700)
        self.proxy = ProxyManager(str(self.binary))
        self.node = ProxyNode("node", "socks", "proxy.invalid", 1080, {"type": "socks", "password": "proxy-secret"})
        self.config = self.proxy.build_singbox_config([{"port": 20001, "node": self.node}])

    def fake_process(self, **kwargs):
        return Mock(stdin=None, stdout=None, stderr=None, **kwargs)

    def test_discovery_requires_an_actual_executable(self):
        missing = ProxyManager(str(self.root / "missing"))
        self.assertIsNone(missing.singbox_bin)
        self.binary.chmod(0o600)
        self.assertIsNone(ProxyManager(str(self.binary)).singbox_bin)

    def test_fetch_failure_clears_nodes_and_does_not_log_url_or_exception_secrets(self):
        self.proxy.set_nodes([self.node])
        with patch("app.proxy_manager.urllib.request.urlopen", side_effect=OSError("secret-exception")), self.assertLogs("freebuff.proxy_manager") as logs:
            nodes = self.proxy.fetch_subscription("https://user:password@subscription.invalid/secret-url")
        self.assertEqual(nodes, [])
        self.assertEqual(self.proxy.nodes, [])
        output = "\n".join(logs.output)
        for secret in ("password", "secret-url", "secret-exception"):
            self.assertNotIn(secret, output)

    def test_find_node_never_falls_back_after_a_filter(self):
        self.proxy.set_nodes([self.node])
        self.assertIs(self.proxy.find_node(), self.node)
        self.assertIsNone(self.proxy.find_node("absent"))
        self.assertIsNone(self.proxy.find_node("["))

    def test_no_direct_outbound_and_no_active_inbound_before_start(self):
        self.assertFalse(any(outbound["type"] == "direct" for outbound in self.config["outbounds"]))
        self.assertEqual(self.proxy.active_inbounds, {})

    def test_start_uses_private_config_bounded_check_and_reaps_on_stop(self):
        process = self.fake_process()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("sing-box", 0.2), 0]
        saved_paths = []

        def check_config(command, **kwargs):
            path = Path(command[-1])
            saved_paths.append(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), self.config)
            self.assertEqual(kwargs["timeout"], 10)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            return SimpleNamespace(returncode=0)

        with patch("app.proxy_manager.subprocess.run", side_effect=check_config), patch("app.proxy_manager.subprocess.Popen", return_value=process) as popen:
            self.assertTrue(self.proxy.start_singbox(self.config))
        self.assertTrue(self.proxy.is_running)
        self.assertEqual(self.proxy.active_inbounds, {20001: "node"})
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.proxy.stop_singbox()
        process.terminate.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        self.assertIsNone(self.proxy.process)
        self.assertIsNone(self.proxy.config_path)
        self.assertFalse(saved_paths[0].exists())

    def test_check_timeout_cleans_private_config_and_never_starts_process(self):
        saved_paths = []

        def timeout(command, **kwargs):
            saved_paths.append(Path(command[-1]))
            raise subprocess.TimeoutExpired("secret-check-command", kwargs["timeout"])

        with patch("app.proxy_manager.subprocess.run", side_effect=timeout), patch("app.proxy_manager.subprocess.Popen") as popen, self.assertLogs("freebuff.proxy_manager") as logs:
            self.assertFalse(self.proxy.start_singbox(self.config))
        popen.assert_not_called()
        self.assertFalse(saved_paths[0].exists())
        self.assertNotIn("secret-check-command", "\n".join(logs.output))
        self.assertEqual(self.proxy.active_inbounds, {})

    def test_process_that_exits_during_startup_is_not_reported_active(self):
        process = self.fake_process()
        process.poll.return_value = 1
        process.wait.return_value = 1
        with patch("app.proxy_manager.subprocess.run", return_value=SimpleNamespace(returncode=0)), patch("app.proxy_manager.subprocess.Popen", return_value=process):
            self.assertFalse(self.proxy.start_singbox(self.config))
        self.assertIsNone(self.proxy.process)
        self.assertIsNone(self.proxy.config_path)
        self.assertFalse(self.proxy.is_running)

    def test_stop_kills_and_waits_again_after_grace_expires(self):
        process = self.fake_process()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("sing-box", 2), 0]
        self.proxy.process = process
        self.proxy.stop_singbox()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        self.assertIsNone(self.proxy.process)

    def test_stop_failure_retains_handle_for_retry(self):
        process = self.fake_process()
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("sing-box", 2)
        self.proxy.process = process
        with self.assertRaises(subprocess.TimeoutExpired):
            self.proxy.stop_singbox()
        self.assertIs(self.proxy.process, process)

    def test_proxy_url_validation_and_redaction(self):
        for url in ("http://user:secret@proxy.invalid:8080", "socks5h://[::1]:1080", "https://proxy.invalid"):
            self.assertEqual(validate_proxy_url(url), url)
        for url in ("", "http://", "file:///tmp/proxy", "http://proxy.invalid:65536", "http://proxy.invalid?token=secret", "http://proxy.invalid\n", "socks5://proxy.invalid/path"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_proxy_url(url)
        self.assertEqual(redact_proxy_url("http://user:secret@[::1]:8080/private?token=secret#secret"), "http://[::1]:8080")


class TestAccountMaintenanceRoutes(unittest.IsolatedAsyncioTestCase):
    async def test_reload_and_refresh_only_call_coordinated_pool_reload(self):
        from app.routes import accounts as routes
        pool = SimpleNamespace(reload=AsyncMock())
        manager = SimpleNamespace(accounts={}, subscription_url="https://subscription.invalid", load_accounts=Mock())
        proxy = SimpleNamespace(nodes=[], active_inbounds={}, fetch_subscription=Mock())
        with patch.object(routes, "global_pool", pool), patch.object(routes, "global_account_manager", manager), patch.object(routes, "global_proxy_manager", proxy):
            self.assertEqual((await routes.reload_accounts())["status"], "success")
            self.assertEqual((await routes.refresh_proxies())["status"], "success")
        self.assertEqual(pool.reload.await_count, 2)
        manager.load_accounts.assert_not_called()
        proxy.fetch_subscription.assert_not_called()

    async def test_reload_preserves_typed_maintenance_errors(self):
        from app.routes import accounts as routes
        error = FreebuffException("Maintenance timed out", status_code=504, error_code="maintenance_timeout")
        with patch.object(routes.global_pool, "reload", new=AsyncMock(side_effect=error)):
            with self.assertRaises(FreebuffException) as caught:
                await routes.reload_accounts()
        self.assertIs(caught.exception, error)


if __name__ == "__main__":
    unittest.main()
