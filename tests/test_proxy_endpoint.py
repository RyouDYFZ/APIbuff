"""Offline checks for local proxy endpoints and account admission failures."""

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.accounts import AccountManager
from app.proxy_manager import BASE_INBOUND_PORT, LocalProxyEndpoint, ProxyManager, ProxyNode, _resolve_proxy_host


class EndpointEnvironmentTestCase(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("FREEBUFF_PROXY_LISTEN_IP", None)
        os.environ.pop("FREEBUFF_PROXY_HOST", None)


class TestBoundedProxyHostResolver(unittest.TestCase):
    def assert_safe_failure(self, failure):
        with patch("app.proxy_manager.subprocess.run", side_effect=failure) as run:
            with self.assertRaises(ValueError) as caught:
                _resolve_proxy_host("proxy.internal")
        message = str(caught.exception)
        self.assertIn("FREEBUFF_PROXY_HOST", message)
        for secret in ("private-command", "private-output", "private-error"):
            self.assertNotIn(secret, message)
        self.assertTrue(caught.exception.__suppress_context__)
        command = run.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command[-1], "proxy.internal")
        options = run.call_args.kwargs
        self.assertFalse(options.get("shell", False))
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertTrue(options["check"])
        self.assertTrue(math.isfinite(options["timeout"]))
        self.assertGreater(options["timeout"], 0)
        self.assertLessEqual(options["timeout"], 5)

    def test_lookup_timeout_returns_safe_error_with_bounded_noninteractive_call(self):
        self.assert_safe_failure(subprocess.TimeoutExpired(
            ["private-command"], 5, output="private-output", stderr="private-error",
        ))

    def test_lookup_nonzero_exit_returns_safe_error_without_subprocess_details(self):
        self.assert_safe_failure(subprocess.CalledProcessError(
            1, ["private-command"], output="private-output", stderr="private-error",
        ))


class TestLocalProxyEndpoint(EndpointEnvironmentTestCase):
    def test_default_endpoint_remains_local_without_name_resolution(self):
        with patch("app.proxy_manager._resolve_proxy_host") as resolve:
            endpoint = LocalProxyEndpoint.from_environment()
        self.assertEqual(endpoint.listen_ip, "127.0.0.1")
        self.assertEqual(endpoint.host, "127.0.0.1")
        self.assertEqual(endpoint.url_for_port(20001), "http://127.0.0.1:20001")
        resolve.assert_not_called()

    def test_empty_host_uses_configured_listen_ip_without_name_resolution(self):
        with patch.dict(os.environ, {
            "FREEBUFF_PROXY_LISTEN_IP": "127.0.0.2",
            "FREEBUFF_PROXY_HOST": "",
        }), patch("app.proxy_manager._resolve_proxy_host") as resolve:
            endpoint = LocalProxyEndpoint.from_environment()
        self.assertEqual(endpoint.listen_ip, "127.0.0.2")
        self.assertEqual(endpoint.url_for_port(20002), "http://127.0.0.2:20002")
        resolve.assert_not_called()

    def test_alias_matching_listener_is_preserved_in_client_url(self):
        with patch.dict(os.environ, {"FREEBUFF_PROXY_HOST": "proxy.internal"}), patch(
            "app.proxy_manager._resolve_proxy_host", return_value={"127.0.0.1"},
        ) as resolve:
            endpoint = LocalProxyEndpoint.from_environment()
        self.assertEqual(endpoint.listen_ip, "127.0.0.1")
        self.assertEqual(endpoint.url_for_port(20001), "http://proxy.internal:20001")
        resolve.assert_called_once_with("proxy.internal")

    def test_alias_must_resolve_only_to_the_exact_listening_address(self):
        for addresses in (
            set(), {"127.0.0.2"}, {"::1"}, {"192.168.1.50"},
            {"127.0.0.1", "::1"}, {"127.0.0.1", "203.0.113.10"},
        ):
            with self.subTest(addresses=addresses), patch.dict(os.environ, {
                "FREEBUFF_PROXY_HOST": "proxy.internal",
            }), patch("app.proxy_manager._resolve_proxy_host", return_value=addresses):
                with self.assertRaises(ValueError):
                    LocalProxyEndpoint.from_environment()

    def test_resolution_failure_never_falls_back_to_literal_loopback(self):
        with patch.dict(os.environ, {"FREEBUFF_PROXY_HOST": "proxy.internal"}), patch(
            "app.proxy_manager._resolve_proxy_host", side_effect=ValueError("Cannot resolve proxy host"),
        ):
            with self.assertRaises(ValueError):
                LocalProxyEndpoint.from_environment()

    def test_literal_host_must_match_listener_without_dns(self):
        with patch("app.proxy_manager._resolve_proxy_host") as resolve:
            for host in ("127.0.0.2", "::1", "203.0.113.10"):
                with self.subTest(host=host), patch.dict(os.environ, {"FREEBUFF_PROXY_HOST": host}):
                    with self.assertRaises(ValueError):
                        LocalProxyEndpoint.from_environment()
        resolve.assert_not_called()

    def test_nonloopback_or_non_ip_listener_is_rejected_before_dns(self):
        values = (
            "0.0.0.0", "::", "192.168.1.1", "203.0.113.10", "localhost",
            "http://127.0.0.1", "127.0.0.1:20001", "::1%lo0",
        )
        with patch("app.proxy_manager._resolve_proxy_host") as resolve:
            for value in values:
                with self.subTest(value=value), patch.dict(os.environ, {
                    "FREEBUFF_PROXY_LISTEN_IP": value, "FREEBUFF_PROXY_HOST": "proxy.internal",
                }):
                    with self.assertRaises(ValueError):
                        LocalProxyEndpoint.from_environment()
        resolve.assert_not_called()

    def test_url_components_and_invalid_domain_names_are_rejected_before_dns(self):
        values = (
            "http://proxy.internal:20001", "proxy.internal:20001", "user@proxy.internal",
            "proxy.internal/path", "proxy.internal?query", "proxy.internal#fragment",
            "proxy internal", "proxy.internal\n", "[::1", "::1]", ".", "proxy..internal",
            "-proxy.internal", "proxy_.internal", "代理.internal", "a" * 64 + ".internal",
        )
        with patch("app.proxy_manager._resolve_proxy_host") as resolve:
            for value in values:
                with self.subTest(value=value), patch.dict(os.environ, {"FREEBUFF_PROXY_HOST": value}):
                    with self.assertRaises(ValueError):
                        LocalProxyEndpoint.from_environment()
        resolve.assert_not_called()

    def test_ipv6_listener_stays_unbracketed_in_config_and_bracketed_in_url(self):
        with patch.dict(os.environ, {"FREEBUFF_PROXY_LISTEN_IP": "::1"}), patch(
            "app.proxy_manager._resolve_proxy_host",
        ) as resolve:
            endpoint = LocalProxyEndpoint.from_environment()
        self.assertEqual(endpoint.listen_ip, "::1")
        self.assertEqual(endpoint.url_for_port(20001), "http://[::1]:20001")
        resolve.assert_not_called()

    def test_ipv6_alias_resolves_to_configured_listener(self):
        with patch.dict(os.environ, {
            "FREEBUFF_PROXY_LISTEN_IP": "::1", "FREEBUFF_PROXY_HOST": "proxy.internal",
        }), patch("app.proxy_manager._resolve_proxy_host", return_value={"::1"}):
            endpoint = LocalProxyEndpoint.from_environment()
        self.assertEqual(endpoint.url_for_port(20001), "http://proxy.internal:20001")

    def test_port_must_be_an_integer_in_the_socket_port_range(self):
        endpoint = LocalProxyEndpoint.from_environment()
        self.assertEqual(endpoint.url_for_port(1), "http://127.0.0.1:1")
        self.assertEqual(endpoint.url_for_port(65535), "http://127.0.0.1:65535")
        for port in (0, -1, 65536, True, False, 20001.0, "20001", None):
            with self.subTest(port=port), self.assertRaises(ValueError):
                endpoint.url_for_port(port)


class TestProxyEndpointConfiguration(EndpointEnvironmentTestCase):
    def setUp(self):
        super().setUp()
        self.proxy = ProxyManager(singbox_bin="/nonexistent/proxy-endpoint-test")
        self.node = ProxyNode("test-node", "socks", "upstream.invalid", 1080, {"type": "socks"})

    def build(self, ports=(20001, 20002)):
        return self.proxy.build_singbox_config([{"port": port, "node": self.node} for port in ports])

    def test_one_configuration_snapshot_drives_all_listeners_and_client_urls(self):
        with patch.dict(os.environ, {"FREEBUFF_PROXY_HOST": "proxy.internal"}), patch(
            "app.proxy_manager._resolve_proxy_host", return_value={"127.0.0.1"},
        ) as resolve:
            config = self.build()
        self.assertEqual([inbound["listen"] for inbound in config["inbounds"]], ["127.0.0.1"] * 2)
        self.assertEqual([inbound["listen_port"] for inbound in config["inbounds"]], [20001, 20002])
        with patch.dict(os.environ, {
            "FREEBUFF_PROXY_LISTEN_IP": "::1", "FREEBUFF_PROXY_HOST": "different.internal",
        }), patch("app.proxy_manager._resolve_proxy_host") as later_resolve:
            self.assertEqual(self.proxy.local_proxy_url(20001), "http://proxy.internal:20001")
            self.assertEqual(self.proxy.local_proxy_url(20002), "http://proxy.internal:20002")
        resolve.assert_called_once_with("proxy.internal")
        later_resolve.assert_not_called()

    def test_rebuilding_configuration_reads_new_endpoint_settings(self):
        self.build()
        with patch.dict(os.environ, {"FREEBUFF_PROXY_LISTEN_IP": "::1"}):
            config = self.build()
        self.assertEqual([inbound["listen"] for inbound in config["inbounds"]], ["::1"] * 2)
        self.assertEqual(self.proxy.local_proxy_url(20001), "http://[::1]:20001")

    def test_invalid_binding_port_is_rejected_during_configuration(self):
        for port in (0, 65536, True, "20001"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                self.build((port,))

    def test_duplicate_ports_leave_no_partial_or_previous_pending_inbounds(self):
        self.build((19999,))
        self.assertEqual(self.proxy._pending_inbounds, {19999: "test-node"})
        with patch.dict(os.environ, {"FREEBUFF_PROXY_LISTEN_IP": "::1"}):
            with self.assertRaises(ValueError):
                self.build((20001, 20001))
        self.assertEqual(self.proxy._pending_inbounds, {})
        self.assertEqual(self.proxy.local_proxy_url(19999), "http://127.0.0.1:19999")


class TestProxyEndpointAccountAdmission(EndpointEnvironmentTestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="proxy-endpoint-accounts-")
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
        seed.start()
        self.addCleanup(seed.stop)
        self.node = ProxyNode("chosen-node", "socks", "upstream.invalid", 1080, {"type": "socks"})
        fetch = patch.object(self.proxy, "fetch_subscription", side_effect=self.fetch_node)
        fetch.start()
        self.addCleanup(fetch.stop)

    def fetch_node(self, _url):
        # One physical exit node per account; supply as many nodes as accounts.
        second = ProxyNode("second-node", "socks", "upstream2.invalid", 1080, {"type": "socks"})
        self.proxy.set_nodes([self.node, second])
        return self.proxy.nodes

    def write_accounts(self, *, explicit=False):
        accounts = [
            {"id": "one", "name": "One", "token": "test-token-one"},
            {"id": "two", "name": "Two", "token": "test-token-two"},
        ]
        if explicit:
            accounts.append({
                "id": "explicit", "name": "Explicit", "token": "test-token-three",
                "proxy": "http://explicit-proxy.invalid:8080",
            })
        self.config_file.write_text(json.dumps({
            "subscription_url": "https://subscription.invalid", "accounts": accounts,
        }))

    def test_started_accounts_use_alias_with_distinct_assigned_ports(self):
        self.write_accounts()
        with patch.dict(os.environ, {"FREEBUFF_PROXY_HOST": "proxy.internal"}), patch(
            "app.proxy_manager._resolve_proxy_host", return_value={"127.0.0.1"},
        ), patch.object(self.proxy, "start_singbox", return_value=True) as start:
            accounts = self.manager.load_accounts()
        self.assertEqual([account.assigned_proxy for account in accounts], [
            f"http://proxy.internal:{BASE_INBOUND_PORT + 1}",
            f"http://proxy.internal:{BASE_INBOUND_PORT + 2}",
        ])
        self.assertEqual(self.manager.get_healthy_accounts(), accounts)
        config = start.call_args.args[0]
        self.assertEqual([inbound["listen"] for inbound in config["inbounds"]], ["127.0.0.1"] * 2)

    def test_alias_mismatch_blocks_every_bound_account(self):
        # Explicit-proxy accounts are chained behind the gateway inbound too,
        # so a broken local endpoint fails closed for them as well.
        self.write_accounts(explicit=True)
        with patch.dict(os.environ, {"FREEBUFF_PROXY_HOST": "proxy.internal"}), patch(
            "app.proxy_manager._resolve_proxy_host", return_value={"192.168.1.50"},
        ), patch.object(self.proxy, "start_singbox") as start:
            accounts = self.manager.load_accounts()
        start.assert_not_called()
        for account in accounts:
            self.assertEqual(account.status, "proxy_error")
            self.assertIsNone(account.assigned_proxy)
        self.assertEqual(self.manager.get_healthy_accounts(), [])

    def test_daemon_failure_does_not_publish_prepared_alias_urls(self):
        self.write_accounts()
        with patch.dict(os.environ, {"FREEBUFF_PROXY_HOST": "proxy.internal"}), patch(
            "app.proxy_manager._resolve_proxy_host", return_value={"127.0.0.1"},
        ), patch.object(self.proxy, "start_singbox", return_value=False):
            accounts = self.manager.load_accounts()
        self.assertEqual([account.status for account in accounts], ["proxy_error"] * 2)
        self.assertEqual([account.assigned_proxy for account in accounts], [None, None])
        self.assertEqual(self.manager.get_healthy_accounts(), [])

    def test_reload_with_changed_resolution_does_not_reuse_previous_alias(self):
        self.write_accounts()
        with patch.dict(os.environ, {"FREEBUFF_PROXY_HOST": "proxy.internal"}), patch(
            "app.proxy_manager._resolve_proxy_host", return_value={"127.0.0.1"},
        ), patch.object(self.proxy, "start_singbox", return_value=True):
            initial = self.manager.load_accounts()
        self.assertTrue(all(account.assigned_proxy for account in initial))
        with patch.dict(os.environ, {"FREEBUFF_PROXY_HOST": "proxy.internal"}), patch(
            "app.proxy_manager._resolve_proxy_host", return_value={"203.0.113.10"},
        ), patch.object(self.proxy, "start_singbox") as start:
            reloaded = self.manager.load_accounts()
        start.assert_not_called()
        self.assertTrue(all(account.status == "proxy_error" for account in reloaded))
        self.assertTrue(all(account.assigned_proxy is None for account in reloaded))
        self.assertEqual(self.manager.get_healthy_accounts(), [])


if __name__ == "__main__":
    unittest.main()
