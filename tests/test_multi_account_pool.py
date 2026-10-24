"""
Integration Test Suite for Multi-Account Orchestration, Multi-Protocol VPN Parsing,
Sing-box Inbound/Outbound Binding, Load Balancing (Least-Busy, Round-Robin), and 429 Transparent Failover.
"""

import os
import sys
import json
import base64
import time
import shutil
import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.proxy_manager import SubscriptionParser, ProxyManager, ProxyNode
from app.accounts import AccountConfig, AccountState, AccountManager
from app.pool import MultiAccountWorkerPool
from app.models import ChatMessage
from app.exceptions import FreebuffRateLimitError, FreebuffAuthError, FreebuffServerError

class TestMultiAccountAndVPN(unittest.TestCase):

    def test_01_clash_subscription_parsing(self):
        """Verifies parsing of Clash/Mihomo YAML subscription with multiple protocols."""
        clash_yaml = """
proxies:
  - name: "US-Hysteria2"
    type: hysteria2
    server: 1.2.3.4
    port: 443
    password: "secret_password"
    sni: "us.example.com"
  - name: "JP-Trojan"
    type: trojan
    server: 2.3.4.5
    port: 443
    password: "trojan_password"
    sni: "jp.example.com"
  - name: "SG-Shadowsocks"
    type: ss
    server: 3.4.5.6
    port: 8388
    cipher: aes-256-gcm
    password: "ss_password"
  - name: "HK-VMess"
    type: vmess
    server: 4.5.6.7
    port: 10086
    uuid: "b831381d-6324-4d53-ad4f-8cda48b30811"
    alterId: 0
    cipher: auto
    tls: true
"""
        nodes = SubscriptionParser.parse_subscription_content(clash_yaml)
        self.assertEqual(len(nodes), 4)
        
        # Check Hysteria2 node
        hy2 = nodes[0]
        self.assertEqual(hy2.name, "US-Hysteria2")
        self.assertEqual(hy2.protocol, "hysteria2")
        self.assertEqual(hy2.server, "1.2.3.4")
        self.assertEqual(hy2.port, 443)

        # Check Trojan node
        trojan = nodes[1]
        self.assertEqual(trojan.name, "JP-Trojan")
        self.assertEqual(trojan.protocol, "trojan")

        # Check Shadowsocks node
        ss = nodes[2]
        self.assertEqual(ss.protocol, "shadowsocks")
        self.assertEqual(ss.raw_config.get("method"), "aes-256-gcm")

        # Check VMess node
        vmess = nodes[3]
        self.assertEqual(vmess.protocol, "vmess")
        self.assertEqual(vmess.raw_config.get("uuid"), "b831381d-6324-4d53-ad4f-8cda48b30811")

    def test_02_base64_v2ray_uri_parsing(self):
        """Verifies parsing of Base64 encoded node list (V2Ray standard format)."""
        vmess_json = {
            "v": "2",
            "ps": "VMess-Node",
            "add": "v.example.com",
            "port": 443,
            "id": "a0000000-0000-0000-0000-000000000001",
            "aid": 0,
            "scy": "auto",
            "net": "tcp",
            "tls": "tls",
            "sni": "v.example.com"
        }
        vmess_uri = "vmess://" + base64.b64encode(json.dumps(vmess_json).encode()).decode()
        ss_uri = "ss://" + base64.b64encode(b"aes-128-gcm:pwd123").decode() + "@1.1.1.1:8388#SS-Node"
        trojan_uri = "trojan://trojanpwd@2.2.2.2:443?sni=trojan.com#Trojan-Node"
        hy2_uri = "hy2://hypwd@3.3.3.3:443?sni=hy2.com#Hy2-Node"

        combined = "\n".join([vmess_uri, ss_uri, trojan_uri, hy2_uri])
        b64_sub = base64.b64encode(combined.encode()).decode()

        nodes = SubscriptionParser.parse_subscription_content(b64_sub)
        self.assertEqual(len(nodes), 4)
        names = [n.name for n in nodes]
        self.assertIn("VMess-Node", names)
        self.assertIn("SS-Node", names)
        self.assertIn("Trojan-Node", names)
        self.assertIn("Hy2-Node", names)

    def test_03_singbox_config_generation_and_validation(self):
        """Verifies Sing-box config generation with isolated inbound ports and routes."""
        pm = ProxyManager()
        node1 = ProxyNode("US-Node", "shadowsocks", "1.2.3.4", 8388, {"type": "shadowsocks", "method": "aes-256-gcm", "password": "pass"})
        node2 = ProxyNode("JP-Node", "trojan", "5.6.7.8", 443, {"type": "trojan", "password": "pass", "tls": {"enabled": True}})

        bindings = [
            {"port": 20001, "node": node1},
            {"port": 20002, "node": node2}
        ]
        cfg = pm.build_singbox_config(bindings)
        
        # Check inbounds
        inbound_ports = [ib["listen_port"] for ib in cfg["inbounds"]]
        self.assertIn(20001, inbound_ports)
        self.assertIn(20002, inbound_ports)

        # Check outbounds
        outbound_tags = [ob["tag"] for ob in cfg["outbounds"]]
        self.assertIn("out_node_1_shadowsocks", outbound_tags)
        self.assertIn("out_node_2_trojan", outbound_tags)

        # Check route rules
        rules = cfg["route"]["rules"]
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0]["inbound"], ["in_acc_1"])
        self.assertEqual(rules[0]["outbound"], "out_node_1_shadowsocks")
        self.assertEqual(rules[1]["inbound"], ["in_acc_2"])
        self.assertEqual(rules[1]["outbound"], "out_node_2_trojan")

        # If sing-box binary exists, test validation
        if pm.singbox_bin and os.path.exists(pm.singbox_bin):
            test_path = "/tmp/test_singbox_generated.json"
            with open(test_path, "w") as f:
                json.dump(cfg, f, indent=2)
            import subprocess
            res = subprocess.run([pm.singbox_bin, "check", "-c", test_path], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"Sing-box validation failed: {res.stderr}")

    def test_04_account_sandbox_isolation(self):
        """Verifies that each account gets an isolated credentials file and sandbox."""
        tmp_accounts_file = "/tmp/test_accounts.json"
        acc_data = {
            "load_balance_strategy": "least_busy",
            "accounts": [
                {
                    "id": "acc_isolated_01",
                    "name": "Account 1",
                    "token": "sk-token-account-01",
                    "proxy": "socks5://127.0.0.1:20001",
                    "enabled": True
                },
                {
                    "id": "acc_isolated_02",
                    "name": "Account 2",
                    "token": "sk-token-account-02",
                    "proxy": "socks5://127.0.0.1:20002",
                    "enabled": True
                }
            ]
        }
        with open(tmp_accounts_file, "w") as f:
            json.dump(acc_data, f)

        am = AccountManager(config_file=tmp_accounts_file)
        accounts = am.load_accounts()
        self.assertEqual(len(accounts), 2)

        # Check sandbox 1
        acc1 = am.get_account("acc_isolated_01")
        creds_file_1 = os.path.join(acc1.home_dir, ".config", "manicode", "credentials.json")
        self.assertTrue(os.path.exists(creds_file_1))
        with open(creds_file_1, "r") as f:
            c1 = json.load(f)
            self.assertEqual(c1["accessToken"], "sk-token-account-01")

        # Check sandbox 2
        acc2 = am.get_account("acc_isolated_02")
        creds_file_2 = os.path.join(acc2.home_dir, ".config", "manicode", "credentials.json")
        self.assertTrue(os.path.exists(creds_file_2))
        with open(creds_file_2, "r") as f:
            c2 = json.load(f)
            self.assertEqual(c2["accessToken"], "sk-token-account-02")

        # Confirm homes are distinct
        self.assertNotEqual(acc1.home_dir, acc2.home_dir)

    def test_05_load_balancing_least_busy_and_round_robin(self):
        """Verifies least-busy and round-robin load balancing selection."""
        pool = MultiAccountWorkerPool()

        # Mock 3 accounts
        acc1 = AccountState(AccountConfig(id="acc_lb_1", name="Acc 1", token="tok1"))
        acc2 = AccountState(AccountConfig(id="acc_lb_2", name="Acc 2", token="tok2"))
        acc3 = AccountState(AccountConfig(id="acc_lb_3", name="Acc 3", token="tok3"))

        # Mock workers
        w1 = MagicMock(is_running=True)
        w2 = MagicMock(is_running=True)
        w3 = MagicMock(is_running=True)

        pool.workers = {"acc_lb_1": w1, "acc_lb_2": w2, "acc_lb_3": w3}

        # Mock global_account_manager
        with patch("app.pool.global_account_manager") as mock_am:
            mock_am.get_healthy_accounts.return_value = [acc1, acc2, acc3]
            mock_am.load_balance_strategy = "least_busy"

            # 1. Initially all have 0 active turns, should pick acc1
            chosen_acc, _ = pool.select_worker(strategy="least_busy")
            self.assertEqual(chosen_acc.config.id, "acc_lb_1")

            # 2. Make acc1 busy (1 turn), acc2 and acc3 free
            acc1.active_turns = 1
            chosen_acc, _ = pool.select_worker(strategy="least_busy")
            self.assertEqual(chosen_acc.config.id, "acc_lb_2")

            # 3. Make acc2 busy (2 turns), acc3 free (0 turns)
            acc2.active_turns = 2
            chosen_acc, _ = pool.select_worker(strategy="least_busy")
            self.assertEqual(chosen_acc.config.id, "acc_lb_3")

            # 4. Test Round-Robin
            r1, _ = pool.select_worker(strategy="round_robin")
            r2, _ = pool.select_worker(strategy="round_robin")
            r3, _ = pool.select_worker(strategy="round_robin")
            r4, _ = pool.select_worker(strategy="round_robin")
            self.assertEqual([r1.config.id, r2.config.id, r3.config.id, r4.config.id],
                             ["acc_lb_1", "acc_lb_2", "acc_lb_3", "acc_lb_1"])

    def test_06_transparent_failover_on_429(self):
        """Verifies transparent failover: Worker 1 raises 429, pool immediately fails over to Worker 2."""
        pool = MultiAccountWorkerPool()

        acc1 = AccountState(AccountConfig(id="acc_fo_1", name="Acc 1", token="tok1"))
        acc2 = AccountState(AccountConfig(id="acc_fo_2", name="Acc 2", token="tok2"))

        w1 = MagicMock(is_running=True)
        w2 = MagicMock(is_running=True)

        # Worker 1 raises RateLimit 429
        w1.execute_turn = AsyncMock(side_effect=FreebuffRateLimitError("Rate limit exceeded"))
        w1.ensure_workspace = AsyncMock()
        w1.ensure_model = AsyncMock()

        # Worker 2 succeeds
        w2.execute_turn = AsyncMock(return_value={"content": "Success response from Account 2"})
        w2.ensure_workspace = AsyncMock()
        w2.ensure_model = AsyncMock()

        pool.workers = {"acc_fo_1": w1, "acc_fo_2": w2}

        async def run_failover():
            with patch("app.pool.global_account_manager") as mock_am:
                mock_am.get_healthy_accounts.return_value = [acc1, acc2]
                mock_am.load_balance_strategy = "least_busy"
                mock_am.get_account.side_effect = lambda aid: {"acc_fo_1": acc1, "acc_fo_2": acc2}.get(aid)

                messages = [ChatMessage(role="user", content="Test prompt")]
                result = await pool.execute_chat(messages)
                return result

        res = asyncio.run(run_failover())
        
        # Verify seamless success response was returned
        self.assertEqual(res["content"], "Success response from Account 2")
        # Verify Account 1 was marked cooling down
        self.assertTrue(acc1.is_cooling)
        self.assertEqual(acc1.status, "cooling")
        self.assertEqual(acc1.failed_requests, 1)
        # Verify Account 2 handled the request
        self.assertEqual(acc2.total_requests, 1)

if __name__ == "__main__":
    unittest.main()
