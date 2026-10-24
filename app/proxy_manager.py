"""
Multi-Protocol VPN / Proxy Manager & Subscription Engine for Freebuff API Gateway.
Supports automatic parsing of subscriptions and node protocols:
Shadowsocks, VMess, VLESS, Trojan, Hysteria 1/2, TUIC, WireGuard, SOCKS5, HTTP/HTTPS.
Integrates with Sing-box to create dedicated, isolated local inbound ports per account.
"""

import os
import sys
import json
import base64
import re
import shutil
import logging
import asyncio
import subprocess
import urllib.request
import urllib.parse
from typing import List, Dict, Any, Optional

try:
    import yaml
except ImportError:
    yaml = None

logger = logging.getLogger("freebuff.proxy_manager")

BASE_INBOUND_PORT = int(os.environ.get("FREEBUFF_BASE_PROXY_PORT", "20000"))

class ProxyNode:
    """Represents a parsed proxy node from a subscription or direct URI."""
    def __init__(
        self,
        name: str,
        protocol: str,
        server: str,
        port: int,
        raw_config: Dict[str, Any],
        latency_ms: Optional[float] = None
    ):
        self.name = name
        self.protocol = protocol.lower()
        self.server = server
        self.port = port
        self.raw_config = raw_config
        self.latency_ms = latency_ms

    def to_singbox_outbound(self, tag: str) -> Dict[str, Any]:
        """Converts internal node definition into standard Sing-box outbound configuration."""
        cfg = {"type": self.protocol, "tag": tag, "server": self.server, "server_port": self.port}
        # Merge protocol-specific attributes
        for k, v in self.raw_config.items():
            if k not in ("name", "server", "port"):
                cfg[k] = v
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "protocol": self.protocol,
            "server": self.server,
            "port": self.port,
            "latency_ms": self.latency_ms
        }


class SubscriptionParser:
    """Parses various subscription formats: Clash YAML, Base64 URI lists, and direct links."""

    @staticmethod
    def parse_subscription_content(content: str) -> List[ProxyNode]:
        nodes: List[ProxyNode] = []
        content = content.strip()
        if not content:
            return nodes

        # 1. Try Clash YAML
        if yaml and ("proxies:" in content or "proxy-providers:" in content):
            try:
                data = yaml.safe_load(content)
                if isinstance(data, dict) and "proxies" in data:
                    for p in data["proxies"]:
                        node = SubscriptionParser._parse_clash_proxy(p)
                        if node:
                            nodes.append(node)
                    if nodes:
                        return nodes
            except Exception as e:
                logger.debug(f"Failed to parse as Clash YAML: {e}")

        # 2. Try Base64 decode (standard V2Ray format)
        decoded = None
        # Add padding if needed
        padding = 4 - (len(content) % 4)
        if padding < 4:
            padded_content = content + ("=" * padding)
        else:
            padded_content = content

        try:
            raw_bytes = base64.b64decode(padded_content)
            decoded = raw_bytes.decode('utf-8', errors='ignore')
        except Exception:
            try:
                raw_bytes = base64.urlsafe_b64decode(padded_content)
                decoded = raw_bytes.decode('utf-8', errors='ignore')
            except Exception:
                decoded = content

        text_to_parse = decoded if decoded and any(p in decoded for p in ("://", "vmess://", "ss://")) else content

        # 3. Parse line-by-line URI links
        lines = text_to_parse.replace('\r', '\n').split('\n')
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            node = SubscriptionParser.parse_uri(line)
            if node:
                nodes.append(node)

        return nodes

    @staticmethod
    def _parse_clash_proxy(p: Dict[str, Any]) -> Optional[ProxyNode]:
        """Maps Clash proxy dictionary to Sing-box compatible ProxyNode."""
        ptype = str(p.get("type", "")).lower()
        name = p.get("name", "node")
        server = p.get("server")
        port = int(p.get("port", 0))
        if not server or not port:
            return None

        raw = {}
        if ptype in ("ss", "shadowsocks"):
            raw["type"] = "shadowsocks"
            raw["method"] = p.get("cipher")
            raw["password"] = p.get("password")
            if p.get("plugin"):
                raw["plugin"] = p.get("plugin")
                raw["plugin_opts"] = p.get("plugin-opts")
        elif ptype in ("vmess",):
            raw["type"] = "vmess"
            raw["uuid"] = p.get("uuid")
            raw["alter_id"] = p.get("alterId", 0)
            raw["security"] = p.get("cipher", "auto")
            if p.get("tls"):
                raw["tls"] = {"enabled": True, "server_name": p.get("servername", server)}
        elif ptype in ("vless",):
            raw["type"] = "vless"
            raw["uuid"] = p.get("uuid")
            raw["flow"] = p.get("flow", "")
            if p.get("tls"):
                raw["tls"] = {"enabled": True, "server_name": p.get("servername", server)}
        elif ptype in ("trojan",):
            raw["type"] = "trojan"
            raw["password"] = p.get("password")
            raw["tls"] = {"enabled": True, "server_name": p.get("sni", server)}
        elif ptype in ("hysteria2", "hy2"):
            raw["type"] = "hysteria2"
            raw["password"] = p.get("password")
            raw["tls"] = {"enabled": True, "server_name": p.get("sni", server)}
        elif ptype in ("tuic",):
            raw["type"] = "tuic"
            raw["uuid"] = p.get("uuid")
            raw["password"] = p.get("password")
        elif ptype in ("socks5", "socks"):
            raw["type"] = "socks"
            if p.get("username"):
                raw["username"] = p.get("username")
                raw["password"] = p.get("password", "")
        elif ptype in ("http", "https"):
            raw["type"] = "http"
            if p.get("username"):
                raw["username"] = p.get("username")
                raw["password"] = p.get("password", "")
            if ptype == "https" or p.get("tls"):
                raw["tls"] = {"enabled": True}
        else:
            raw["type"] = ptype

        return ProxyNode(name=name, protocol=raw.get("type", ptype), server=server, port=port, raw_config=raw)

    @staticmethod
    def parse_uri(uri: str) -> Optional[ProxyNode]:
        """Parses individual URI links (vmess://, ss://, trojan://, hy2://, etc.)."""
        try:
            if uri.startswith("vmess://"):
                b64_part = uri[len("vmess://"):]
                b64_part += "=" * ((4 - len(b64_part) % 4) % 4)
                v_json = json.loads(base64.b64decode(b64_part).decode('utf-8'))
                name = v_json.get("ps", "vmess-node")
                server = v_json.get("add")
                port = int(v_json.get("port", 443))
                raw = {
                    "type": "vmess",
                    "uuid": v_json.get("id"),
                    "alter_id": int(v_json.get("aid", 0)),
                    "security": v_json.get("scy", "auto")
                }
                if v_json.get("tls") in ("tls", "1", True):
                    raw["tls"] = {"enabled": True, "server_name": v_json.get("sni") or v_json.get("host") or server}
                return ProxyNode(name=name, protocol="vmess", server=server, port=port, raw_config=raw)

            elif uri.startswith("ss://"):
                # ss://[base64(method:password)]@host:port#name or ss://base64(method:password@host:port)#name
                main_part, _, tag = uri[5:].partition("#")
                name = urllib.parse.unquote(tag) if tag else "ss-node"
                if "@" in main_part:
                    cred, host_port = main_part.split("@", 1)
                    cred += "=" * ((4 - len(cred) % 4) % 4)
                    method_pw = base64.b64decode(cred).decode('utf-8', errors='ignore')
                    method, password = method_pw.split(":", 1)
                    host, port = host_port.split(":", 1)
                else:
                    main_part += "=" * ((4 - len(main_part) % 4) % 4)
                    decoded = base64.b64decode(main_part).decode('utf-8', errors='ignore')
                    method_pw, host_port = decoded.split("@", 1)
                    method, password = method_pw.split(":", 1)
                    host, port = host_port.split(":", 1)
                raw = {"type": "shadowsocks", "method": method, "password": password}
                return ProxyNode(name=name, protocol="shadowsocks", server=host, port=int(port), raw_config=raw)

            elif uri.startswith("trojan://"):
                # trojan://password@host:port?sni=...#name
                parsed = urllib.parse.urlparse(uri)
                name = urllib.parse.unquote(parsed.fragment) or "trojan-node"
                password = parsed.username
                server = parsed.hostname
                port = parsed.port or 443
                params = urllib.parse.parse_qs(parsed.query)
                sni = params.get("sni", [server])[0]
                raw = {"type": "trojan", "password": password, "tls": {"enabled": True, "server_name": sni}}
                return ProxyNode(name=name, protocol="trojan", server=server, port=port, raw_config=raw)

            elif uri.startswith("hysteria2://") or uri.startswith("hy2://"):
                prefix = "hysteria2://" if uri.startswith("hysteria2://") else "hy2://"
                parsed = urllib.parse.urlparse(uri)
                name = urllib.parse.unquote(parsed.fragment) or "hy2-node"
                password = parsed.username
                server = parsed.hostname
                port = parsed.port or 443
                params = urllib.parse.parse_qs(parsed.query)
                sni = params.get("sni", [server])[0]
                raw = {"type": "hysteria2", "password": password, "tls": {"enabled": True, "server_name": sni}}
                return ProxyNode(name=name, protocol="hysteria2", server=server, port=port, raw_config=raw)

            elif uri.startswith("socks5://") or uri.startswith("socks://"):
                parsed = urllib.parse.urlparse(uri)
                name = urllib.parse.unquote(parsed.fragment) or "socks-node"
                raw = {"type": "socks"}
                if parsed.username:
                    raw["username"] = parsed.username
                    raw["password"] = parsed.password or ""
                return ProxyNode(name=name, protocol="socks", server=parsed.hostname, port=parsed.port or 1080, raw_config=raw)

            elif uri.startswith("http://") or uri.startswith("https://"):
                parsed = urllib.parse.urlparse(uri)
                name = urllib.parse.unquote(parsed.fragment) or "http-node"
                raw = {"type": "http"}
                if parsed.username:
                    raw["username"] = parsed.username
                    raw["password"] = parsed.password or ""
                if uri.startswith("https://"):
                    raw["tls"] = {"enabled": True}
                return ProxyNode(name=name, protocol="http", server=parsed.hostname, port=parsed.port or (443 if uri.startswith("https://") else 80), raw_config=raw)
        except Exception as e:
            logger.debug(f"Failed to parse URI '{uri[:30]}...': {e}")
        return None


class ProxyManager:
    """Orchestrates Sing-box instance, parses subscriptions, and manages local proxy inbounds."""

    def __init__(self, singbox_bin: Optional[str] = None):
        self.singbox_bin = (
            singbox_bin or
            shutil.which("sing-box") or
            "/opt/homebrew/bin/sing-box" or
            "/usr/local/bin/sing-box" or
            "/usr/bin/sing-box"
        )
        self.nodes: List[ProxyNode] = []
        self.active_inbounds: Dict[int, str] = {}  # port -> outbound node tag
        self.process: Optional[subprocess.Popen] = None
        self.config_path = "/tmp/freebuff_singbox_runtime.json"
        self._lock = asyncio.Lock()

    def fetch_subscription(self, url: str) -> List[ProxyNode]:
        """Fetches and parses a remote subscription URL."""
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "ClashMeta/1.18.0 sing-box/1.14.0 FreebuffGateway/1.0"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode('utf-8', errors='ignore')
                self.nodes = SubscriptionParser.parse_subscription_content(content)
                logger.info(f"Successfully fetched {len(self.nodes)} proxy nodes from subscription.")
                return self.nodes
        except Exception as e:
            logger.error(f"Failed to fetch subscription from '{url}': {e}")
            return []

    def set_nodes(self, nodes: List[ProxyNode]):
        self.nodes = nodes

    def find_node(self, filter_pattern: Optional[str] = None) -> Optional[ProxyNode]:
        """Finds a matching node by regex/substring, or falls back to first node."""
        if not self.nodes:
            return None
        if not filter_pattern:
            return self.nodes[0]

        pattern = re.compile(filter_pattern, re.IGNORECASE)
        for node in self.nodes:
            if pattern.search(node.name) or pattern.search(node.protocol):
                return node
        return self.nodes[0]

    def build_singbox_config(self, account_bindings: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Builds a complete Sing-box configuration file.
        Creates one mixed inbound (HTTP/SOCKS5) per account, strictly routed to its paired outbound.
        account_bindings: list of {"port": 20001, "node": ProxyNode}
        """
        inbounds = []
        outbounds = [{"type": "direct", "tag": "direct"}]
        rules = []

        seen_nodes = set()
        self.active_inbounds.clear()

        for idx, binding in enumerate(account_bindings):
            port = binding["port"]
            node: ProxyNode = binding["node"]
            in_tag = f"in_acc_{idx + 1}"
            out_tag = f"out_node_{idx + 1}_{node.protocol}"

            inbounds.append({
                "type": "mixed",
                "tag": in_tag,
                "listen": "127.0.0.1",
                "listen_port": port
            })

            outbounds.append(node.to_singbox_outbound(out_tag))
            rules.append({
                "inbound": [in_tag],
                "outbound": out_tag
            })

            self.active_inbounds[port] = node.name

        config = {
            "log": {
                "level": "warn",
                "timestamp": True
            },
            "inbounds": inbounds,
            "outbounds": outbounds,
            "route": {
                "rules": rules
            }
        }
        return config

    def start_singbox(self, config: Dict[str, Any]) -> bool:
        """Writes configuration and starts the Sing-box daemon process."""
        if not self.singbox_bin or not os.path.exists(self.singbox_bin):
            logger.warning(f"sing-box binary not found at '{self.singbox_bin}'. Local proxy daemon disabled.")
            return False

        self.stop_singbox()

        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        # Validate config first
        check = subprocess.run([self.singbox_bin, "check", "-c", self.config_path], capture_output=True, text=True)
        if check.returncode != 0:
            logger.error(f"Invalid Sing-box configuration: {check.stderr}")
            return False

        logger.info(f"Starting Sing-box daemon with {len(self.active_inbounds)} inbounds...")
        self.process = subprocess.Popen(
            [self.singbox_bin, "run", "-c", self.config_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE
        )
        return True

    def stop_singbox(self):
        """Stops the running Sing-box daemon cleanly."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
            logger.info("Sing-box daemon stopped.")


# Global singleton proxy manager
global_proxy_manager = ProxyManager()
