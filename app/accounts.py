"""
Multi-Account Management & Sandbox Isolation Engine for Freebuff API Gateway.
Manages per-account credentials, isolated home sandboxes, VPN node pairing, and telemetry.
"""

import os
import json
import time
import logging
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from .proxy_manager import global_proxy_manager, ProxyNode, BASE_INBOUND_PORT

logger = logging.getLogger("freebuff.accounts")

ACCOUNTS_FILE = os.environ.get("FREEBUFF_ACCOUNTS_FILE", "accounts.json")
BASE_SANDBOX_DIR = os.environ.get("FREEBUFF_SANDBOX_DIR", "/tmp/freebuff_accounts")

class AccountConfig(BaseModel):
    id: str = Field(..., description="Unique account identifier")
    name: str = Field(..., description="Friendly display name")
    token: str = Field(..., description="Codebuff/Freebuff API Access Token")
    proxy: Optional[str] = Field(None, description="Explicit HTTP/SOCKS5 proxy (e.g. socks5://127.0.0.1:1080)")
    node_filter: Optional[str] = Field(None, description="Regex/keyword filter to pair with subscription node")
    weight: int = Field(1, description="Load balancing weight")
    enabled: bool = Field(True, description="Whether this account is active")


class AccountState:
    """Tracks runtime state, active sessions, and cooldowns for a specific account."""
    def __init__(self, config: AccountConfig):
        self.config = config
        self.assigned_proxy: Optional[str] = config.proxy
        self.assigned_node_name: Optional[str] = None
        self.home_dir = os.path.join(BASE_SANDBOX_DIR, config.id)
        self.status: str = "idle"  # idle, busy, cooling, auth_error, disabled
        self.active_turns: int = 0
        self.total_requests: int = 0
        self.failed_requests: int = 0
        self.cooldown_until: float = 0.0
        self.balance: Optional[float] = None
        self.last_used: float = 0.0
        self.last_error: Optional[str] = None

    @property
    def is_cooling(self) -> bool:
        return time.time() < self.cooldown_until

    def mark_cooldown(self, seconds: float = 60.0, reason: Optional[str] = None):
        self.cooldown_until = time.time() + seconds
        self.status = "cooling"
        self.last_error = reason
        logger.warning(f"Account '{self.config.id}' marked cooling for {seconds}s: {reason}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.config.id,
            "name": self.config.name,
            "enabled": self.config.enabled,
            "weight": self.config.weight,
            "status": "cooling" if self.is_cooling else self.status,
            "assigned_proxy": self.assigned_proxy,
            "assigned_node_name": self.assigned_node_name,
            "active_turns": self.active_turns,
            "total_requests": self.total_requests,
            "failed_requests": self.failed_requests,
            "cooling_remaining_s": max(0, int(self.cooldown_until - time.time())) if self.is_cooling else 0,
            "balance": self.balance,
            "last_used": self.last_used,
            "last_error": self.last_error
        }


class AccountManager:
    """Manages the lifecycle of accounts, reads accounts.json, and pairs them with proxies."""

    def __init__(self, config_file: str = ACCOUNTS_FILE):
        self.config_file = config_file
        self.subscription_url: Optional[str] = None
        self.load_balance_strategy: str = "least_busy"  # least_busy, round_robin, quota_aware
        self.accounts: Dict[str, AccountState] = {}
        self._ensure_sandbox_root()

    def _ensure_sandbox_root(self):
        os.makedirs(BASE_SANDBOX_DIR, exist_ok=True)

    def load_accounts(self) -> List[AccountState]:
        """Loads accounts from accounts.json or falls back to single-account environment variable."""
        self.accounts.clear()

        raw_accounts: List[AccountConfig] = []
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self.subscription_url = data.get("subscription_url")
                        self.load_balance_strategy = data.get("load_balance_strategy", "least_busy")
                        acc_list = data.get("accounts", [])
                    elif isinstance(data, list):
                        acc_list = data
                    else:
                        acc_list = []

                    for a in acc_list:
                        raw_accounts.append(AccountConfig(**a))
                logger.info(f"Loaded {len(raw_accounts)} accounts from '{self.config_file}'.")
            except Exception as e:
                logger.error(f"Error reading accounts configuration from '{self.config_file}': {e}")

        # Fallback to single account if no accounts configured
        if not raw_accounts:
            token = os.environ.get("CODEBUFF_API_KEY")
            if not token:
                # Try reading from ~/.config/manicode/credentials.json
                default_creds = os.path.expanduser("~/.config/manicode/credentials.json")
                if os.path.exists(default_creds):
                    try:
                        with open(default_creds, "r") as f:
                            token = json.load(f).get("accessToken")
                    except Exception:
                        pass

            raw_accounts.append(AccountConfig(
                id="default",
                name="Default Local Account",
                token=token or "unauthenticated",
                proxy=os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"),
                enabled=True
            ))
            logger.info("Operating in single-account backward compatibility mode.")

        # 1. Fetch remote subscription if configured
        if self.subscription_url:
            global_proxy_manager.fetch_subscription(self.subscription_url)

        # 2. Pair accounts with proxies and prepare sandboxes
        singbox_bindings = []
        next_port = BASE_INBOUND_PORT + 1

        for acc in raw_accounts:
            state = AccountState(acc)
            if acc.enabled:
                if acc.proxy:
                    # User specified explicit proxy URL
                    state.assigned_proxy = acc.proxy
                    state.assigned_node_name = "Explicit Proxy"
                elif global_proxy_manager.nodes:
                    # Match node from subscription
                    matched_node = global_proxy_manager.find_node(acc.node_filter)
                    if matched_node:
                        port = next_port
                        next_port += 1
                        singbox_bindings.append({"port": port, "node": matched_node})
                        # Mixed inbound accepts both SOCKS5 and HTTP
                        state.assigned_proxy = f"http://127.0.0.1:{port}"
                        state.assigned_node_name = matched_node.name

                # Pre-seed isolated credentials in sandbox
                self._seed_sandbox(state)

            self.accounts[acc.id] = state

        # 3. If we have Sing-box bindings, build config and start daemon
        if singbox_bindings and global_proxy_manager.singbox_bin:
            config = global_proxy_manager.build_singbox_config(singbox_bindings)
            global_proxy_manager.start_singbox(config)

        return list(self.accounts.values())

    def _seed_sandbox(self, state: AccountState):
        """Creates isolated user directory and writes credentials.json."""
        config_dir = os.path.join(state.home_dir, ".config", "manicode")
        os.makedirs(config_dir, exist_ok=True)
        creds_file = os.path.join(config_dir, "credentials.json")
        try:
            with open(creds_file, "w", encoding="utf-8") as f:
                json.dump({
                    "accessToken": state.config.token,
                    "tokenStore": "file"
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to seed credentials for account '{state.config.id}': {e}")

    def get_account(self, account_id: str) -> Optional[AccountState]:
        return self.accounts.get(account_id)

    def get_healthy_accounts(self) -> List[AccountState]:
        return [
            acc for acc in self.accounts.values()
            if acc.config.enabled and not acc.is_cooling and acc.status != "auth_error"
        ]


# Global singleton account manager
global_account_manager = AccountManager()
