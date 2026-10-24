"""
Multi-Account Management & Sandbox Isolation Engine for Freebuff API Gateway.
Manages per-account credentials, isolated home sandboxes, VPN node pairing, and telemetry.
"""

import os
import json
import time
import logging
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field, field_validator

from .proxy_manager import global_proxy_manager, BASE_INBOUND_PORT, validate_proxy_url, redact_proxy_url
from .credentials import (
    account_config_dir,
    default_config_dir,
    read_cli_auth_token,
    write_cli_credentials,
)

logger = logging.getLogger("freebuff.accounts")

ACCOUNTS_FILE = os.environ.get("FREEBUFF_ACCOUNTS_FILE", "accounts.json")
BASE_SANDBOX_DIR = os.environ.get("FREEBUFF_SANDBOX_DIR", "/tmp/freebuff_accounts")

class AccountConfig(BaseModel):
    id: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=128, description="Unique account identifier and sandbox directory name")
    name: str = Field(..., description="Friendly display name")
    token: str = Field(..., description="Codebuff/Freebuff API Access Token")
    proxy: Optional[str] = Field(None, description="Explicit HTTP/SOCKS5 proxy (e.g. socks5://127.0.0.1:1080)")
    node_filter: Optional[str] = Field(None, description="Regex/keyword filter to pair with subscription node")
    weight: int = Field(1, description="Load balancing weight")
    enabled: bool = Field(True, description="Whether this account is active")

    @field_validator("proxy")
    @classmethod
    def validate_explicit_proxy(cls, value):
        return validate_proxy_url(value) if value is not None else None


class AccountState:
    """Tracks runtime state, active sessions, and cooldowns for a specific account."""
    def __init__(self, config: AccountConfig):
        self.config = config
        self.assigned_proxy: Optional[str] = config.proxy
        self.assigned_node_name: Optional[str] = None
        self.home_dir = os.path.abspath(os.path.join(BASE_SANDBOX_DIR, config.id))
        self.status: str = "idle" if config.enabled else "disabled"
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
            "assigned_proxy": redact_proxy_url(self.assigned_proxy),
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
        if os.path.islink(BASE_SANDBOX_DIR):
            raise ValueError("Account sandbox root must not be a symlink")
        os.makedirs(BASE_SANDBOX_DIR, mode=0o700, exist_ok=True)
        os.chmod(BASE_SANDBOX_DIR, 0o700)

    def load_accounts(self) -> List[AccountState]:
        """Loads accounts from accounts.json or falls back to single-account environment variable."""
        raw_accounts: List[AccountConfig] = []
        subscription_url = None
        load_balance_strategy = "least_busy"
        has_account_file = os.path.exists(self.config_file)
        if has_account_file:
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        subscription_url = data.get("subscription_url")
                        if subscription_url is not None:
                            if not isinstance(subscription_url, str) or not subscription_url.strip():
                                raise ValueError("Subscription URL must be a nonempty string")
                            subscription_url = subscription_url.strip()
                        load_balance_strategy = data.get("load_balance_strategy", "least_busy")
                        acc_list = data.get("accounts", [])
                    elif isinstance(data, list):
                        acc_list = data
                    else:
                        raise ValueError("Accounts configuration must be an object or array")

                    for a in acc_list:
                        account = AccountConfig(**a)
                        if account.enabled and (
                            not account.token.strip()
                            or account.token.strip() == "unauthenticated"
                        ):
                            raise ValueError("Enabled accounts require an explicit token")
                        raw_accounts.append(account)
                    # macOS commonly uses a case-insensitive filesystem; IDs
                    # differing only by case would otherwise share credentials.
                    ids = [account.id.casefold() for account in raw_accounts]
                    if len(ids) != len(set(ids)):
                        raise ValueError("Account IDs must be unique")
                logger.info(f"Loaded {len(raw_accounts)} accounts from '{self.config_file}'.")
            except (OSError, ValueError, TypeError):
                # Validation errors may include the complete input (including tokens).
                # A bad explicit configuration must never silently select another account.
                raise ValueError("Invalid accounts configuration; check the file, required fields, and unique safe account IDs") from None

        # Fallback only when no account file was supplied. An empty file disables accounts.
        if not has_account_file:
            token = os.environ.get("CODEBUFF_API_KEY") or os.environ.get("FREEBUFF_AUTH_TOKEN")
            if not token:
                token = read_cli_auth_token(default_config_dir())

            raw_accounts.append(AccountConfig(
                id="default",
                name="Default Local Account",
                token=token or "",
                proxy=os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"),
                enabled=True
            ))
            logger.info("Operating in single-account backward compatibility mode.")

        # Loading runs only after the pool's maintenance gate has drained workers.
        # A failed required proxy never turns into implicit direct connectivity.
        subscription_ready = False
        if subscription_url:
            try:
                subscription_ready = bool(global_proxy_manager.fetch_subscription(subscription_url))
            except Exception:
                logger.error("Proxy subscription refresh failed.")
            if not subscription_ready:
                global_proxy_manager.set_nodes([])
        else:
            # Removing a subscription must not keep its previous nodes available.
            global_proxy_manager.set_nodes([])

        # 2. Pair accounts with proxies and prepare sandboxes
        singbox_bindings = []
        bound_accounts = []
        next_port = BASE_INBOUND_PORT + 1
        new_accounts = {}

        for acc in raw_accounts:
            state = AccountState(acc)
            if acc.enabled:
                if acc.proxy:
                    # User specified explicit proxy URL
                    state.assigned_proxy = acc.proxy
                    state.assigned_node_name = "Explicit Proxy"
                elif subscription_url or acc.node_filter is not None:
                    # Match node from subscription
                    matched_node = global_proxy_manager.find_node(acc.node_filter) if subscription_ready else None
                    if matched_node:
                        port = next_port
                        next_port += 1
                        singbox_bindings.append({"port": port, "node": matched_node})
                        state.assigned_node_name = matched_node.name
                        bound_accounts.append(state)
                    else:
                        state.status = "proxy_error"
                        state.last_error = "Required proxy is unavailable: subscription failed or no node matched."

                # Pre-seed isolated credentials in sandbox
                self._seed_sandbox(state)

            new_accounts[acc.id] = state

        # 3. If we have Sing-box bindings, build config and start daemon
        if singbox_bindings:
            proxy_started = False
            proxy_error = "Required proxy is unavailable: sing-box could not start."
            try:
                config = global_proxy_manager.build_singbox_config(singbox_bindings)
                # Prepare every URL from the exact endpoint used by the config.
                # Publish them only after the daemon has successfully started.
                proxy_urls = [global_proxy_manager.local_proxy_url(binding["port"]) for binding in singbox_bindings]
                proxy_started = global_proxy_manager.start_singbox(config) is True
            except ValueError:
                proxy_error = "Required proxy is unavailable: invalid local endpoint; check FREEBUFF_PROXY_LISTEN_IP, FREEBUFF_PROXY_HOST resolution and inbound ports."
                logger.error(proxy_error)
            except Exception:
                logger.error("Required proxy daemon could not start.")
            if proxy_started:
                for state, proxy_url in zip(bound_accounts, proxy_urls):
                    state.assigned_proxy = proxy_url
            else:
                for state in bound_accounts:
                    state.status = "proxy_error"
                    state.assigned_proxy = None
                    state.last_error = proxy_error

        self.accounts = new_accounts
        self.subscription_url = subscription_url
        self.load_balance_strategy = load_balance_strategy
        return list(self.accounts.values())

    def _seed_sandbox(self, state: AccountState):
        """Prepare private directories and seed the CLI's supported credential format."""
        for directory in (
            state.home_dir,
            os.path.join(state.home_dir, ".config"),
        ):
            if os.path.islink(directory):
                raise ValueError("Account sandbox directories must not be symlinks")
            os.makedirs(directory, mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)

        write_cli_credentials(account_config_dir(state.home_dir), state.config.token)

    def get_account(self, account_id: str) -> Optional[AccountState]:
        return self.accounts.get(account_id)

    def get_healthy_accounts(self) -> List[AccountState]:
        return [
            acc for acc in self.accounts.values()
            if acc.config.enabled and not acc.is_cooling and acc.status in {"idle", "busy", "cooling"}
        ]


# Global singleton account manager
global_account_manager = AccountManager()
