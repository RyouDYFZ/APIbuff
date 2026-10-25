"""Offline regressions for proxy-level telemetry blocking.

The official CLI ships active PostHog telemetry and honours no environment
switch, so the gateway's sing-box daemon is the enforcement point: every
account egresses through a gateway-managed inbound whose first route rule
refuses the telemetry domains.
"""

import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.accounts import AccountManager
from app.proxy_manager import (
    ProxyManager,
    ProxyNode,
    TELEMETRY_BLOCK_DOMAIN_SUFFIXES,
    TELEMETRY_BLOCK_OUTBOUND_TAG,
)

POSTHOG_HOSTS = (
    "us.i.posthog.com",
    "us-assets.i.posthog.com",
    "eu.i.posthog.com",
    "app.posthog.com",
)


def _matches_suffix(host, suffixes):
    """The suffix semantics sing-box domain_suffix applies to a host."""
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


class TelemetryBlockRuleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proxy = ProxyManager(singbox_bin=str(self.root / "no-binary"))

    def build(self, nodes=("socks",)):
        bindings = []
        for index, protocol in enumerate(nodes):
            node = ProxyNode(f"node-{index}", protocol, "upstream.invalid", 1080,
                             {"type": protocol})
            bindings.append({"port": 20001 + index, "node": node})
        return self.proxy.build_singbox_config(bindings)

    def test_every_posthog_host_is_covered_by_the_first_rule(self):
        for host in POSTHOG_HOSTS:
            with self.subTest(host=host):
                self.assertTrue(_matches_suffix(host, TELEMETRY_BLOCK_DOMAIN_SUFFIXES))
        self.assertFalse(_matches_suffix("notposthog.com", TELEMETRY_BLOCK_DOMAIN_SUFFIXES))
        self.assertFalse(_matches_suffix("posthog.com.evil.invalid", TELEMETRY_BLOCK_DOMAIN_SUFFIXES))

    def test_reject_rule_precedes_every_account_route(self):
        config = self.build(("socks", "trojan", "direct"))
        rules = config["route"]["rules"]
        # The legacy outbound-field dialect: valid on the container-pinned
        # sing-box 1.10 as well as newer releases that accept rule actions.
        self.assertEqual(rules[0]["outbound"], TELEMETRY_BLOCK_OUTBOUND_TAG)
        self.assertEqual(rules[0]["domain_suffix"], list(TELEMETRY_BLOCK_DOMAIN_SUFFIXES))
        self.assertEqual(config["outbounds"][0]["type"], "block")
        self.assertTrue(all("inbound" in rule for rule in rules[1:]))
        self.assertEqual([rule["outbound"] for rule in rules[1:]],
                         ["out_node_1_socks", "out_node_2_trojan", "out_node_3_direct"])

    def test_direct_and_explicit_nodes_become_supported_outbounds(self):
        direct = ProxyNode.direct().to_singbox_outbound("out_direct")
        self.assertEqual(direct, {"type": "direct", "tag": "out_direct"})
        endpoint = "socks5h://user%40x:pa%40ss@10.0.0.9:9050"
        explicit = ProxyNode.from_proxy_url(endpoint).to_singbox_outbound("out_explicit")
        self.assertEqual(explicit["type"], "socks")
        self.assertEqual(explicit["server"], "10.0.0.9")
        self.assertEqual(explicit["server_port"], 9050)
        self.assertEqual(explicit["username"], "user@x")
        self.assertEqual(explicit["password"], "pa@ss")

    def test_explicit_proxy_url_mapping_and_rejections(self):
        https_node = ProxyNode.from_proxy_url("https://proxy.invalid")
        self.assertEqual(https_node.protocol, "http")
        self.assertEqual(https_node.to_singbox_outbound("o")["tls"], {"enabled": True})
        plain = ProxyNode.from_proxy_url("http://proxy.invalid:8123")
        self.assertEqual(plain.to_singbox_outbound("o")["server_port"], 8123)
        socks = ProxyNode.from_proxy_url("socks://proxy.invalid")
        self.assertEqual(socks.to_singbox_outbound("o")["server_port"], 1080)
        for bad in ("file:///tmp/x", "http:///no-host", "ftp://proxy.invalid:1"):
            with self.subTest(bad), self.assertRaises(ValueError):
                ProxyNode.from_proxy_url(bad)


class AccountBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_file = self.root / "accounts.json"
        sandbox = patch("app.accounts.BASE_SANDBOX_DIR", str(self.root / "accounts"))
        sandbox.start()
        self.addCleanup(sandbox.stop)
        seed = patch.object(AccountManager, "_seed_sandbox")
        seed.start()
        self.addCleanup(seed.stop)
        self.manager = AccountManager(str(self.config_file))

    def load(self, accounts, subscription=None):
        config = {"accounts": accounts}
        if subscription is not None:
            config["subscription_url"] = subscription
        self.config_file.write_text(json.dumps(config))
        with patch("app.accounts.global_proxy_manager") as proxy:
            proxy.start_singbox.return_value = True
            proxy.local_proxy_url.side_effect = [
                f"http://127.0.0.1:{20001 + index}" for index in range(len(accounts))
            ]
            loaded = self.manager.load_accounts()
            self.bindings = proxy.build_singbox_config.call_args.args[0]
        return loaded

    def test_proxyless_accounts_still_egress_through_the_gateway_inbound(self):
        accounts = self.load([
            {"id": "solo", "name": "Solo", "token": "tok"},
        ])
        self.assertEqual(accounts[0].status, "idle")
        self.assertEqual(accounts[0].assigned_proxy, "http://127.0.0.1:20001")
        self.assertEqual(accounts[0].assigned_node_name, "Direct (gateway-managed)")
        self.assertEqual(len(self.bindings), 1)
        self.assertEqual(
            self.bindings[0]["node"].to_singbox_outbound("check")["type"], "direct")

    def test_daemon_failure_fails_closed_for_proxyless_accounts_too(self):
        # Without the gateway inbound there is no telemetry block, so a
        # missing daemon must never degrade into direct CLI egress.
        self.config_file.write_text(json.dumps({"accounts": [
            {"id": "solo", "name": "Solo", "token": "tok"},
        ]}))
        with patch("app.accounts.global_proxy_manager") as proxy:
            proxy.start_singbox.return_value = False
            proxy.local_proxy_url.side_effect = ["http://127.0.0.1:20001"]
            accounts = self.manager.load_accounts()
        self.assertEqual(accounts[0].status, "proxy_error")
        self.assertIsNone(accounts[0].assigned_proxy)


if __name__ == "__main__":
    unittest.main()
