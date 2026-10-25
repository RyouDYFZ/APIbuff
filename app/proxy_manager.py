"""
Multi-Protocol VPN / Proxy Manager & Subscription Engine for Freebuff API Gateway.
Supports automatic parsing of subscriptions and node protocols:
Shadowsocks, VMess, VLESS, Trojan, Hysteria 1/2, TUIC, WireGuard, SOCKS5, HTTP/HTTPS.
Integrates with Sing-box to create dedicated, isolated local inbound ports per account.
"""

import os
import json
import base64
from dataclasses import dataclass
import ipaddress
import re
import shutil
import logging
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import urllib.request
import urllib.parse
from typing import List, Dict, Any, Optional

try:
    import yaml
except ImportError:
    yaml = None

logger = logging.getLogger("freebuff.proxy_manager")

BASE_INBOUND_PORT = int(os.environ.get("FREEBUFF_BASE_PROXY_PORT", "20000"))
SINGBOX_CHECK_TIMEOUT_SECONDS = 10
SINGBOX_STARTUP_GRACE_SECONDS = 0.2
PROXY_HOST_LOOKUP_TIMEOUT_SECONDS = 5

# The official CLI ships active PostHog telemetry and honours no environment
# switch, so the gateway's proxy is the enforcement point: every request for
# these domains is refused before it can leave the machine. A suffix entry
# covers every subdomain, which is what the CLI's us.i/us-assets.i hosts need.
TELEMETRY_BLOCK_DOMAIN_SUFFIXES = ("posthog.com",)
TELEMETRY_BLOCK_OUTBOUND_TAG = "telemetry-block"


def _resolve_proxy_host(host: str) -> set[str]:
    """Bound the system resolver without leaving a blocked resolver thread behind."""
    lookup = (
        "import json, socket, sys; "
        "print(json.dumps(sorted({item[4][0] for item in "
        "socket.getaddrinfo(sys.argv[1], None, type=socket.SOCK_STREAM)})))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", lookup, host],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=PROXY_HOST_LOOKUP_TIMEOUT_SECONDS, check=True,
        )
        addresses = json.loads(result.stdout)
        if not isinstance(addresses, list) or not addresses:
            raise ValueError
        return {str(ipaddress.ip_address(address)) for address in addresses}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise ValueError("FREEBUFF_PROXY_HOST could not be resolved within five seconds") from None


@dataclass(frozen=True)
class LocalProxyEndpoint:
    """One validated address pair for both the listener and worker proxy URLs."""

    listen_ip: str = "127.0.0.1"
    host: str = "127.0.0.1"

    @classmethod
    def from_environment(cls) -> "LocalProxyEndpoint":
        listen_raw = os.environ.get("FREEBUFF_PROXY_LISTEN_IP") or "127.0.0.1"
        try:
            listen = ipaddress.ip_address(listen_raw)
            if not listen.is_loopback or "%" in listen_raw:
                raise ValueError
        except ValueError:
            raise ValueError("FREEBUFF_PROXY_LISTEN_IP must be a loopback IP address") from None

        host = os.environ.get("FREEBUFF_PROXY_HOST") or str(listen)
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        try:
            host_ip = ipaddress.ip_address(host)
        except ValueError:
            # Reject URLs, ports, userinfo, whitespace and non-ASCII names before
            # invoking the resolver. Operators can supply IDNA names as punycode.
            labels = host.removesuffix(".").split(".")
            if len(host) > 253 or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in labels
            ):
                raise ValueError("FREEBUFF_PROXY_HOST must be an IP address or hostname, without a port or scheme") from None
            host = host.lower()
            addresses = _resolve_proxy_host(host)
        else:
            host = str(host_ip)
            addresses = {host}

        if addresses != {str(listen)}:
            raise ValueError("FREEBUFF_PROXY_HOST must resolve only to FREEBUFF_PROXY_LISTEN_IP")
        return cls(listen_ip=str(listen), host=host)

    def url_for_port(self, port: int) -> str:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Local proxy port must be an integer between 1 and 65535")
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{port}"


def validate_proxy_url(value: str) -> str:
    """Validate an explicit proxy endpoint without echoing its credentials."""
    try:
        if not value or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme not in {"http", "https", "socks", "socks5", "socks5h"}
            or not parsed.hostname
            or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Proxy must be a valid HTTP(S) or SOCKS5 endpoint URL") from None
    return value


def redact_proxy_url(value: Optional[str]) -> Optional[str]:
    """Expose endpoint identity while keeping userinfo and other URL secrets private."""
    if not value:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        if not host:
            return "[invalid proxy]"
        host = f"[{host}]" if ":" in host else host
        endpoint = f"{host}:{parsed.port}" if parsed.port is not None else host
        return urllib.parse.urlunsplit((parsed.scheme, endpoint, "", "", ""))
    except (TypeError, ValueError):
        return "[invalid proxy]"

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
        if self.protocol == "direct":
            return {"type": "direct", "tag": tag}
        cfg = {"type": self.protocol, "tag": tag, "server": self.server, "server_port": self.port}
        # Merge protocol-specific attributes
        for k, v in self.raw_config.items():
            if k not in ("name", "server", "port"):
                cfg[k] = v
        return cfg

    @classmethod
    def direct(cls, name: str = "Direct (gateway-managed)") -> "ProxyNode":
        """A passthrough node: egress via the host, still behind the block rules."""
        return cls(name=name, protocol="direct", server="", port=0, raw_config={})

    @classmethod
    def from_proxy_url(cls, url: str, name: str = "Explicit Proxy") -> "ProxyNode":
        """Turn an operator-supplied proxy URL into a Sing-box outbound node.

        Chaining the explicit proxy behind the gateway's own inbound keeps the
        telemetry block rules on every account, including ones whose egress is
        an external proxy the gateway does not control.
        """
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme == "https":
            protocol, default_port = "http", 443
        elif scheme == "http":
            protocol, default_port = "http", 80
        elif scheme in ("socks", "socks5", "socks5h"):
            protocol, default_port = "socks", 1080
        else:
            raise ValueError("Unsupported explicit proxy scheme")
        if not parsed.hostname:
            raise ValueError("Explicit proxy URL has no host")
        port = parsed.port or default_port
        raw: Dict[str, Any] = {"server": parsed.hostname, "port": port}
        if parsed.username is not None:
            raw["username"] = urllib.parse.unquote(parsed.username)
        if parsed.password is not None:
            raw["password"] = urllib.parse.unquote(parsed.password)
        if scheme == "https":
            raw["tls"] = {"enabled": True}
        return cls(name=name, protocol=protocol, server=parsed.hostname,
                   port=port, raw_config=raw)

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
            except Exception:
                logger.debug("Failed to parse subscription as Clash YAML.")

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
        except Exception:
            logger.debug("Failed to parse proxy URI.")
        return None


class ProxyManager:
    """Orchestrates Sing-box instance, parses subscriptions, and manages local proxy inbounds."""

    def __init__(self, singbox_bin: Optional[str] = None):
        candidates = [singbox_bin] if singbox_bin else [
            shutil.which("sing-box"), "/opt/homebrew/bin/sing-box",
            "/usr/local/bin/sing-box", "/usr/bin/sing-box",
        ]
        self.singbox_bin = next((
            path for path in candidates
            if path and os.path.isfile(path) and os.access(path, os.X_OK)
        ), None)
        self.nodes: List[ProxyNode] = []
        self.active_inbounds: Dict[int, str] = {}  # port -> outbound node tag
        self._pending_inbounds: Dict[int, str] = {}
        self.process: Optional[subprocess.Popen] = None
        self.config_path: Optional[str] = None
        self.local_endpoint = LocalProxyEndpoint()

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def fetch_subscription(self, url: str) -> List[ProxyNode]:
        """Fetches and parses a remote subscription URL."""
        self.nodes = []
        try:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("Invalid subscription endpoint")
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "ClashMeta/1.18.0 sing-box/1.14.0 FreebuffGateway/1.0"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode('utf-8', errors='ignore')
                self.nodes = SubscriptionParser.parse_subscription_content(content)
                logger.info(f"Successfully fetched {len(self.nodes)} proxy nodes from subscription.")
                return self.nodes
        except Exception:
            # URLs and exception text can contain subscription tokens/passwords.
            self.nodes = []
            logger.error("Failed to fetch or parse proxy subscription.")
            return []

    def set_nodes(self, nodes: List[ProxyNode]):
        self.nodes = nodes

    def find_node(self, filter_pattern: Optional[str] = None,
                  exclude_names: Optional[set] = None) -> Optional[ProxyNode]:
        """Select the first node only when no filter was requested; fail closed otherwise.

        ``exclude_names`` lets callers keep one physical exit node per account:
        two accounts sharing an exit IP is both a correlation signal and the
        direct trigger for the upstream per-network session cap.
        """
        if not self.nodes:
            return None
        excluded = set(exclude_names or ())
        if filter_pattern is None:
            first_available = next((n for n in self.nodes if n.name not in excluded), None)
            return first_available
        if not filter_pattern.strip():
            return None
        try:
            pattern = re.compile(filter_pattern, re.IGNORECASE)
        except re.error:
            logger.warning("Invalid proxy node filter; no node selected.")
            return None
        for node in self.nodes:
            if node.name in excluded:
                continue
            if pattern.search(node.name) or pattern.search(node.protocol):
                return node
        return None

    def build_singbox_config(self, account_bindings: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Builds a complete Sing-box configuration file.
        Creates one mixed inbound (HTTP/SOCKS5) per account, strictly routed to its paired outbound.
        account_bindings: list of {"port": 20001, "node": ProxyNode}

        The telemetry block rule is always first: sing-box evaluates route
        rules in order, so PostHog egress is refused for every inbound before
        any account's outbound is considered. The config uses the legacy
        outbound-field dialect because the container pins sing-box 1.10,
        which rejects the newer rule-action form.
        """
        inbounds = []
        outbounds = [{"type": "block", "tag": TELEMETRY_BLOCK_OUTBOUND_TAG}]
        rules = [{
            "domain_suffix": list(TELEMETRY_BLOCK_DOMAIN_SUFFIXES),
            "outbound": TELEMETRY_BLOCK_OUTBOUND_TAG,
        }]

        self._pending_inbounds.clear()
        endpoint = LocalProxyEndpoint.from_environment()
        pending_inbounds = {}

        for idx, binding in enumerate(account_bindings):
            port = binding["port"]
            endpoint.url_for_port(port)
            if port in pending_inbounds:
                raise ValueError("Local proxy inbound ports must be unique")
            node: ProxyNode = binding["node"]
            in_tag = f"in_acc_{idx + 1}"
            out_tag = f"out_node_{idx + 1}_{node.protocol}"

            inbounds.append({
                "type": "mixed",
                "tag": in_tag,
                "listen": endpoint.listen_ip,
                "listen_port": port
            })

            outbounds.append(node.to_singbox_outbound(out_tag))
            rules.append({
                "inbound": [in_tag],
                "outbound": out_tag
            })

            pending_inbounds[port] = node.name

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
        self.local_endpoint = endpoint
        self._pending_inbounds = pending_inbounds
        return config

    def local_proxy_url(self, port: int) -> str:
        """Use the same validated endpoint snapshot as the generated config."""
        return self.local_endpoint.url_for_port(port)

    def _pid_file(self) -> Path:
        return Path(tempfile.gettempdir()) / f"freebuff-singbox-{os.getuid()}.pid"

    @staticmethod
    def _process_name(pid: int) -> str:
        """Best-effort process name lookup; empty when the process is gone."""
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8") as stream:
                return stream.read().strip()
        except OSError:
            pass
        if sys.platform == "darwin":
            try:
                return subprocess.check_output(
                    ["/bin/ps", "-o", "comm=", "-p", str(pid)],
                    timeout=2, stderr=subprocess.DEVNULL,
                ).decode().strip()
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        return ""

    def _reap_orphaned_singbox(self) -> None:
        """Terminate a daemon left behind by a SIGKILLed gateway.

        Without this, the orphan holds every inbound port and every later
        start fails into proxy_error until someone hunts it down by hand.
        Only a process whose name still contains sing-box is signalled, so a
        recycled PID can never be killed by accident.
        """
        pid_file = self._pid_file()
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            try:
                pid_file.unlink()
            except OSError:
                pass
            return
        if pid > 1 and "sing-box" in self._process_name(pid).lower():
            logger.warning("Reaping orphaned sing-box daemon (pid=%s) from a previous gateway.", pid)
            for send in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(pid, send)
                except OSError:
                    break
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        break
                    time.sleep(0.05)
                else:
                    continue
                break
        try:
            pid_file.unlink()
        except OSError:
            pass

    def start_singbox(self, config: Dict[str, Any]) -> bool:
        """Start a validated daemon and report failure before any worker is launched."""
        pending_inbounds = dict(self._pending_inbounds)
        self.stop_singbox()
        self._reap_orphaned_singbox()
        if not self.singbox_bin or not os.path.isfile(self.singbox_bin) or not os.access(self.singbox_bin, os.X_OK):
            logger.warning("Executable sing-box binary not found; required proxies are unavailable.")
            return False
        try:
            descriptor, self.config_path = tempfile.mkstemp(prefix="freebuff-singbox-", suffix=".json")
            with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
                os.fchmod(config_file.fileno(), 0o600)
                json.dump(config, config_file, indent=2)

            check = subprocess.run(
                [self.singbox_bin, "check", "-c", self.config_path],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=SINGBOX_CHECK_TIMEOUT_SECONDS,
            )
            if check.returncode != 0:
                logger.error("Sing-box configuration validation failed.")
                self.stop_singbox()
                return False

            self.process = subprocess.Popen(
                [self.singbox_bin, "run", "-c", self.config_path],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            try:
                self.process.wait(timeout=SINGBOX_STARTUP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                if self.is_running:
                    try:
                        self._pid_file().write_text(f"{self.process.pid}\n", encoding="utf-8")
                    except OSError:
                        pass
                    self.active_inbounds = pending_inbounds
                    logger.info("Sing-box daemon started with %s inbounds.", len(self.active_inbounds))
                    return True
            logger.error("Sing-box exited during startup.")
        except (OSError, ValueError, subprocess.SubprocessError):
            logger.error("Sing-box validation or startup failed.")
        self.stop_singbox()
        return False

    def stop_singbox(self):
        """Reap the owned daemon before discarding its handle or private configuration."""
        if self.process is not None:
            process = self.process
            try:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            except ProcessLookupError:
                process.wait(timeout=2)
            # If reaping fails, retain the handle and propagate to pool maintenance.
            self.process = None
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            logger.info("Sing-box daemon stopped.")
        try:
            self._pid_file().unlink()
        except OSError:
            pass
        self.active_inbounds.clear()
        if self.config_path is not None:
            try:
                os.unlink(self.config_path)
            except FileNotFoundError:
                pass
            self.config_path = None


# Global singleton proxy manager
global_proxy_manager = ProxyManager()
