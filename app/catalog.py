import os
import json
import time
import base64
import hashlib
import re
import logging
import math
import threading
from copy import deepcopy
from .auth_probe import API_HOST, read_auth_context
from typing import Dict, Any, List, Optional
import requests
try:
    from cryptography.hazmat.primitives.serialization import load_der_private_key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    load_der_private_key = None

logger = logging.getLogger("freebuff.catalog")

FREEBUFF_PER_HOUR = 5

# Local reference catalog. Prices and account eligibility are not verified.
DEFAULT_FALLBACK_CATALOG = {
    "rows": [
        {
            "key": "m-7e20df6765",
            "handle": "z-ai/glm-5.3-flash",
            "displayName": "GLM 5.3 Flash",
            "tagline": "Fast, high-efficiency intelligent reasoning model (Default Freebuff Model)",
            "contextWindow": 128000,
            "multimodal": False,
            "access": "open",
            "premium": False,
            "defaultPrice": 0,
            "badges": [{"label": "Freebuff Default"}, {"label": "Fast"}]
        },
        {
            "key": "m-00032eaeec",
            "handle": "mimo-v2.6-flash-fast",
            "displayName": "Mimo v2.6 Flash Fast",
            "tagline": "Ultra-low latency code generation and lightweight editing",
            "contextWindow": 128000,
            "multimodal": False,
            "access": "open",
            "premium": False,
            "defaultPrice": 0,
            "badges": [{"label": "Free"}]
        },
        {
            "key": "m-096e75164d",
            "handle": "deepseek/deepseek-v4.1-flash",
            "displayName": "DeepSeek v4.1 Flash",
            "tagline": "Advanced reasoning and multi-language software architecture",
            "contextWindow": 128000,
            "multimodal": False,
            "access": "open",
            "premium": False,
            "defaultPrice": 5,
            "badges": [{"label": "Popular"}, {"label": "Reasoning"}]
        },
        {
            "key": "m-127314bb39",
            "handle": "deepseek/deepseek-v4.1-flash-fast",
            "displayName": "DeepSeek v4.1 Flash Fast",
            "tagline": "Accelerated reasoning inference for interactive pair-programming",
            "contextWindow": 128000,
            "multimodal": False,
            "access": "open",
            "premium": False,
            "defaultPrice": 5,
            "badges": [{"label": "Fast"}]
        },
        {
            "key": "m-cb71f819fe",
            "handle": "mimo/mimo-2.6-pro",
            "displayName": "Mimo 2.6 Pro",
            "tagline": "Professional-grade coding model with extended repository context",
            "contextWindow": 256000,
            "multimodal": True,
            "access": "open",
            "premium": True,
            "defaultPrice": 15,
            "badges": [{"label": "Pro"}]
        },
        {
            "key": "m-5a5d0e255e",
            "handle": "openai/gpt-6-luna",
            "displayName": "GPT-6 Luna",
            "tagline": "Frontier intelligence, deep architectural synthesis, and complex planning",
            "contextWindow": 256000,
            "multimodal": True,
            "access": "open",
            "premium": True,
            "defaultPrice": 25,
            "badges": [{"label": "Frontier"}]
        },
        {
            "key": "m-6adcd6d671",
            "handle": "openai/gpt-6.1-sol",
            "displayName": "GPT-6.1 Sol",
            "tagline": "Ultra-capacity multi-agent autonomous engineering model",
            "contextWindow": 512000,
            "multimodal": True,
            "access": "open",
            "premium": True,
            "defaultPrice": 35,
            "badges": [{"label": "Ultra"}]
        },
        {
            "key": "m-94d83cc7e6",
            "handle": "google/gemini-4-argon",
            "displayName": "Gemini 4 Argon",
            "tagline": "Next-generation multimodal model with ultra-long 1M token context",
            "contextWindow": 1000000,
            "multimodal": True,
            "access": "open",
            "premium": True,
            "defaultPrice": 30,
            "badges": [{"label": "1M Context"}]
        },
        {
            "key": "m-5dafce7f08",
            "handle": "google/gemini-3.8-flash",
            "displayName": "Gemini 3.8 Flash",
            "tagline": "High-throughput, rapid token stream multimodal model",
            "contextWindow": 512000,
            "multimodal": True,
            "access": "open",
            "premium": False,
            "defaultPrice": 5,
            "badges": [{"label": "Fast"}]
        },
        {
            "key": "m-79293c5b7d",
            "handle": "anthropic/claude-haiku-5.5",
            "displayName": "Claude Haiku 5.5",
            "tagline": "High-precision code syntax, tool use, and succinct instruction following",
            "contextWindow": 200000,
            "multimodal": True,
            "access": "open",
            "premium": False,
            "defaultPrice": 10,
            "badges": [{"label": "Accurate"}]
        },
        {
            "key": "m-5b6a604eb6",
            "handle": "anthropic/claude-haiku-5.5-100k",
            "displayName": "Claude Haiku 5.5 100k",
            "tagline": "Specialized 100k compact context window for fast turn-around",
            "contextWindow": 100000,
            "multimodal": True,
            "access": "open",
            "premium": False,
            "defaultPrice": 10,
            "badges": []
        },
        {
            "key": "m-9a7e098cc1",
            "handle": "upstage/solar-pro-4",
            "displayName": "Solar Pro 4",
            "tagline": "Domain-adapted enterprise coding model",
            "contextWindow": 128000,
            "multimodal": False,
            "access": "open",
            "premium": False,
            "defaultPrice": 10,
            "badges": []
        },
        {
            "key": "m-69307952f8",
            "handle": "upstage/solar-mini-4",
            "displayName": "Solar Mini 4",
            "tagline": "Lightweight, highly optimized local pair-programmer",
            "contextWindow": 64000,
            "multimodal": False,
            "access": "open",
            "premium": False,
            "defaultPrice": 0,
            "badges": [{"label": "Free"}]
        },
        {
            "key": "m-ddc687c50f",
            "handle": "nvidia/nemotron-3.5-lightning",
            "displayName": "Nemotron 3.5 Lightning",
            "tagline": "High-performance inference with accelerated execution",
            "contextWindow": 128000,
            "multimodal": False,
            "access": "open",
            "premium": False,
            "defaultPrice": 5,
            "badges": []
        },
        {
            "key": "m-e0ae3cb3e2",
            "handle": "google/gemini-3.8-pro-preview",
            "displayName": "Gemini 3.8 Pro Preview",
            "tagline": "Experimental enterprise reasoning and broad coding capabilities",
            "contextWindow": 1000000,
            "multimodal": True,
            "access": "open",
            "premium": True,
            "defaultPrice": 25,
            "badges": [{"label": "Preview"}]
        },
        {
            "key": "m-6d0ac71639",
            "handle": "z-ai/glm-5.5",
            "displayName": "GLM 5.5",
            "tagline": "Flagship multi-step reasoning and deep mathematics",
            "contextWindow": 256000,
            "multimodal": True,
            "access": "open",
            "premium": True,
            "defaultPrice": 20,
            "badges": [{"label": "Flagship"}]
        },
        {
            "key": "m-8d40fa364e",
            "handle": "deepseek/deepseek-v5-preview",
            "displayName": "DeepSeek v5 Preview",
            "tagline": "Next-gen experimental deep architecture and verification",
            "contextWindow": 256000,
            "multimodal": True,
            "access": "open",
            "premium": True,
            "defaultPrice": 25,
            "badges": [{"label": "Preview"}]
        }
    ]
}

def b64u_decode(s: str) -> bytes:
    s += '=' * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s)

def b64u_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip('=')

def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r'[\(\)·]', '', text)
    text = re.sub(r'[^a-z0-9.]+', '-', text)
    return text.strip('-')


def _finite_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _valid_json(value):
    # requests' JSON decoder accepts NaN/Infinity. They are not JSON values and
    # can also hide in passthrough refund, policy, or subscription metadata.
    try:
        json.dumps(value, allow_nan=False)
        return True
    except (ValueError, TypeError, OverflowError):
        return False

class FreebuffCatalogService:
    """Read-only catalog for the default credential profile, never all accounts."""
    def __init__(self, config_dir: Optional[str] = None, api_host: str = API_HOST):
        self._configured_dir = config_dir
        self.config_dir = config_dir
        self.api_host = api_host.rstrip("/")
        self.key_path = None
        self.token = None
        self.key_id = None
        self.priv_key = None
        self._credential_identity = None
        self._lock = threading.RLock()
        self._cached_catalog = deepcopy(DEFAULT_FALLBACK_CATALOG)
        self._cached_session = None
        self._cache_time = 0.0
        self._cache_ttl = 60.0
        self._source = "local_fallback"
        self._error = None
        self._registration_status = "unknown"
        self._slug_to_row = {}
        self._key_to_row = {}
        # Importing the service (including public /health) never reads credentials.
        self._rebuild_index()

    def _load_credentials(self):
        self.token, self.key_id, self.priv_key = None, None, None
        self._registration_status = "unknown"
        context = read_auth_context(self._configured_dir)
        self.config_dir = context["config_dir"]
        self.key_path = os.path.join(self.config_dir, "device-key.json")
        self.token = context["token"]
        token_digest = hashlib.sha256(self.token.encode()).hexdigest() if self.token else None
        account_id = context["account_id"]
        self._scope = (
            f"{self.api_host} user:{account_id}" if account_id
            else f"{self.api_host} token:{token_digest[:32]}" if token_digest else None
        )
        private_digest = None
        if self.token:
            self._registration_status = "unregistered"
            try:
                with open(self.key_path, encoding="utf-8") as handle:
                    record = json.load(handle)
                registrations = record.get("registrations", {})
                key_id = registrations.get(self._scope) if isinstance(registrations, dict) else None
                private = record.get("privateKey")
                if isinstance(key_id, str) and key_id and isinstance(private, str) and load_der_private_key:
                    key = load_der_private_key(b64u_decode(private), password=None)
                    if not isinstance(key, Ed25519PrivateKey):
                        raise ValueError("Unsupported device key type")
                    self.key_id, self.priv_key = key_id, key
                    private_digest = hashlib.sha256(private.encode()).hexdigest()
                    self._registration_status = "registered"
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError, AttributeError):
                self._registration_status = "unreadable"
        else:
            self._registration_status = "missing_credentials"
        identity = (self.config_dir, self.api_host, token_digest, account_id, self.key_id, private_digest)
        changed = identity != self._credential_identity
        self._credential_identity = identity
        return changed

    def _sign_headers(self, method: str, path: str, body: bytes = b"", fetch_id: Optional[str] = None) -> Dict[str, str]:
        if not self.token or not self.key_id or self.priv_key is None:
            raise ValueError("No registered device key for the current credential scope")
        timestamp = int(time.time() * 1000)
        payload = f"freebuff-device-v1\n{method.upper()}\n{path}\n{timestamp}\n{hashlib.sha256(body).hexdigest()}\n{fetch_id or ''}".encode()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "x-freebuff-catalog-protocol": "1",
            "x-freebuff-client": "cli",
            "x-freebuff-device-key": self.key_id,
            "x-freebuff-device-ts": str(timestamp),
            "x-freebuff-device-sig": b64u_encode(self.priv_key.sign(payload)),
        }
        if fetch_id:
            headers["x-freebuff-catalog-fetch"] = fetch_id
        return headers

    @staticmethod
    def _valid_catalog(catalog):
        if not isinstance(catalog, dict) or not _valid_json(catalog):
            return False
        rows = catalog.get("rows")
        if not isinstance(rows, list):
            return False
        for row in rows:
            if not isinstance(row, dict) or any(
                not isinstance(row.get(key), str) or not row[key]
                for key in ("key", "displayName", "handle")
            ):
                return False
            for key in ("tagline", "access", "dataUse"):
                if key in row and not isinstance(row[key], str):
                    return False
            for key in ("premium", "multimodal"):
                if key in row and not isinstance(row[key], bool):
                    return False
            context = row.get("contextWindow", 128000)
            if not _finite_number(context) or context <= 0 or int(context) != context:
                return False
            if row.get("defaultPrice") is not None and not _finite_number(row["defaultPrice"]):
                return False
            if row.get("defaultEffort") is not None and not isinstance(row["defaultEffort"], str):
                return False
            efforts = row.get("efforts", [])
            if not isinstance(efforts, list) or any(not isinstance(value, str) for value in efforts):
                return False
            badges = row.get("badges", [])
            if not isinstance(badges, list) or any(
                not isinstance(badge, dict) or not isinstance(badge.get("label"), str) for badge in badges
            ):
                return False
        return True

    @staticmethod
    def _valid_session(session):
        if not isinstance(session, dict) or not _valid_json(session) or not any(key in session for key in ("freebucks", "accessTier", "countryCode", "countryVerified", "subscription")):
            return False
        for key in ("freebucks", "subscription"):
            if session.get(key) is not None and not isinstance(session[key], dict):
                return False
        freebucks = session.get("freebucks") or {}
        for key in ("prices", "priceNotices", "offPeak", "daily", "wallet"):
            if freebucks.get(key) is not None and not isinstance(freebucks[key], dict):
                return False
        plans = freebucks.get("planRequiredModelIds")
        if plans is not None and (not isinstance(plans, list) or any(not isinstance(key, str) for key in plans)):
            return False
        for section, keys in (
            (freebucks, ("balance",)),
            (freebucks.get("daily") or {}, ("limit", "spent", "remaining")),
            (freebucks.get("wallet") or {}, ("balance", "monthlyBonus")),
        ):
            if any(section.get(key) is not None and not _finite_number(section[key]) for key in keys):
                return False
        if any(value is not None and not _finite_number(value) for value in (freebucks.get("prices") or {}).values()):
            return False
        if any(value is not None and not isinstance(value, str) for value in (freebucks.get("priceNotices") or {}).values()):
            return False
        if any(value is not None and not isinstance(value, dict) for value in (freebucks.get("offPeak") or {}).values()):
            return False
        daily = freebucks.get("daily") or {}
        if any(daily.get(key) is not None and not isinstance(daily[key], str) for key in ("resetAt", "resetTimeZone")):
            return False
        refund = freebucks.get("lastRefund")
        if refund is not None and (
            not isinstance(refund, dict) or not isinstance(refund.get("id"), str)
            or not refund["id"] or not _finite_number(refund.get("daily"))
            or not _finite_number(refund.get("wallet"))
        ):
            return False
        for key in ("accessTier", "countryCode"):
            if session.get(key) is not None and not isinstance(session[key], str):
                return False
        if session.get("countryVerified") is not None and not isinstance(session["countryVerified"], bool):
            return False
        subscription = session.get("subscription") or {}
        tiers = subscription.get("tiers")
        return tiers is None or (isinstance(tiers, list) and all(isinstance(tier, dict) for tier in tiers))

    def refresh(self, force: bool = False):
        with self._lock:
            now = time.monotonic()
            try:
                changed = self._load_credentials()
            except Exception:
                # Do not retain the preceding profile's credentials or balances.
                self.token, self.key_id, self.priv_key = None, None, None
                self._registration_status = "unreadable"
                changed = True
            if not force and not changed and self._cache_time and now - self._cache_time < self._cache_ttl:
                return
            self._cached_catalog = deepcopy(DEFAULT_FALLBACK_CATALOG)
            self._cached_session = None
            self._source = "local_fallback"
            self._error = None
            try:
                if not self.token or not self.key_id or self.priv_key is None:
                    self._error = {"code": self._registration_status, "message": "No registered signing key is available for the default credential scope."}
                    return
                path = "/api/v1/freebuff/models"
                response = requests.get(self.api_host + path, headers=self._sign_headers("GET", path), timeout=3.5)
                if response.status_code != 200:
                    self._error = {"code": "catalog_unavailable", "http_status": response.status_code}
                    return
                catalog = response.json()
                if not self._valid_catalog(catalog):
                    self._error = {"code": "invalid_catalog_response"}
                    return
                self._cached_catalog, self._source = catalog, "upstream_catalog"
                path = "/api/v1/freebuff/session"
                fetch_id = catalog.get("fetchId")
                response = requests.get(
                    self.api_host + path,
                    headers=self._sign_headers("GET", path, fetch_id=fetch_id if isinstance(fetch_id, str) else None),
                    timeout=3.5,
                )
                if response.status_code == 200:
                    session = response.json()
                    if self._valid_session(session):
                        self._cached_session = session
                    else:
                        self._error = {"code": "invalid_session_response"}
                else:
                    self._error = {"code": "session_unavailable", "http_status": response.status_code}
            except Exception:
                self._cached_session = None
                self._error = {"code": "refresh_unavailable", "message": "The scoped upstream refresh could not be completed."}
            finally:
                self._cache_time = time.monotonic()
                try:
                    self._rebuild_index()
                except Exception:
                    self._cached_catalog = deepcopy(DEFAULT_FALLBACK_CATALOG)
                    self._cached_session = None
                    self._source = "local_fallback"
                    self._error = {"code": "invalid_catalog_response"}
                    self._rebuild_index()

    def _rebuild_index(self):
        catalog = self._cached_catalog
        session = self._cached_session or {}
        freebucks = session.get("freebucks")
        freebucks = freebucks if isinstance(freebucks, dict) else {}
        prices = freebucks.get("prices") or {}
        notices = freebucks.get("priceNotices") or {}
        plans = freebucks.get("planRequiredModelIds")
        off_peak = freebucks.get("offPeak") or {}
        slug_map, key_map = {}, {}
        fallback = self._source == "local_fallback"
        for row in catalog.get("rows", []):
            key, handle, display_name = row["key"], row["handle"], row["displayName"]
            primary_slug = handle.rsplit("/", 1)[-1] or slugify(display_name)
            # Only this model's own names are aliases. Cross-generation names
            # such as gpt-4o must never silently select a different model.
            aliases = sorted({key, handle, primary_slug, slugify(display_name)})
            price = None if fallback else prices.get(key, row.get("defaultPrice"))
            price = price if _finite_number(price) else None
            enriched = {
                "key": key, "handle": handle, "displayName": display_name,
                "tagline": row.get("tagline", ""), "primarySlug": primary_slug, "aliases": aliases,
                "access": "unknown" if fallback else row.get("access", "unknown"),
                "planRequired": (
                    None if fallback else True if row.get("access") == "locked"
                    else key in plans if isinstance(plans, list) else None
                ),
                "premium": row.get("premium", False), "dataUse": row.get("dataUse", "service"),
                "multimodal": row.get("multimodal", False), "contextWindow": row.get("contextWindow", 128000),
                "efforts": row.get("efforts", []), "defaultEffort": row.get("defaultEffort"),
                "badges": [] if fallback else row.get("badges", []),
                "source": self._source, "status": "unverified" if fallback else "catalog_available",
                "error": self._error,
                "pricing": {
                    "freebucks_per_hour": price,
                    "hours_equivalent": price / FREEBUFF_PER_HOUR if price is not None else None,
                    "is_free": price == 0 if price is not None else None,
                    "notice": "Local fallback model metadata; price and account eligibility are unverified." if fallback else notices.get(key),
                    "off_peak": off_peak.get(key),
                },
            }
            key_map[key.lower()] = enriched
            for alias in aliases:
                slug_map[alias.lower()] = enriched
        self._key_to_row, self._slug_to_row = key_map, slug_map

    @staticmethod
    def _format_model(row):
        return {
            "id": row["primarySlug"], "object": "model", "created": 1700000000, "owned_by": "freebuff",
            "key": row["key"], "display_name": row["displayName"], "tagline": row["tagline"],
            "aliases": row["aliases"], "pricing": row["pricing"], "source": row["source"],
            "status": row["status"], "error": row["error"],
            "capabilities": {
                "context_window": row["contextWindow"], "multimodal": row["multimodal"],
                "efforts": row["efforts"], "default_effort": row["defaultEffort"],
                "access": row["access"], "plan_required": row["planRequired"],
                "premium": row["premium"], "data_use": row["dataUse"],
                "badges": [badge.get("label") for badge in row["badges"] if isinstance(badge, dict) and isinstance(badge.get("label"), str)],
            },
        }

    def _models(self):
        return deepcopy(sorted(
            (self._format_model(row) for row in self._key_to_row.values()),
            key=lambda model: (model["pricing"]["freebucks_per_hour"] is None, model["pricing"]["freebucks_per_hour"] or 0, model["id"]),
        ))

    def get_all_models(self) -> List[Dict[str, Any]]:
        with self._lock:
            self.refresh()
            return self._models()

    def get_catalog_overview(self):
        with self._lock:
            self.refresh()
            return {"object": "list", "data": self._models(), "source": self._source,
                    "status": "unverified" if self._source == "local_fallback" else "available", "error": deepcopy(self._error)}

    def get_model(self, requested: str):
        with self._lock:
            self.refresh()
            row = self._slug_to_row.get((requested or "").strip().lower())
            return deepcopy(self._format_model(row)) if row else None

    def get_pricing_overview(self) -> Dict[str, Any]:
        with self._lock:
            self.refresh()
            session = self._cached_session or {}
            fb = session.get("freebucks")
            fb = fb if isinstance(fb, dict) else {}
            daily, wallet = fb.get("daily") or {}, fb.get("wallet") or {}
            def number(value):
                return value if _finite_number(value) else None
            def hours(value):
                value = number(value)
                return round(value / FREEBUFF_PER_HOUR, 2) if value is not None else None
            account = None
            if any(session.get(key) is not None for key in ("accessTier", "countryCode", "countryVerified")):
                account = {"access_tier": session.get("accessTier"), "country_code": session.get("countryCode"),
                           "country_verified": session.get("countryVerified"), "source": "upstream_session", "status": "available"}
            balance = None
            if any(number(value) is not None for value in (fb.get("balance"), daily.get("limit"), daily.get("spent"), daily.get("remaining"), wallet.get("balance"))):
                balance = {
                    "total_freebucks": number(fb.get("balance")), "total_hours": hours(fb.get("balance")),
                    "daily": {
                        "limit_freebucks": number(daily.get("limit")), "limit_hours": hours(daily.get("limit")),
                        "spent_freebucks": number(daily.get("spent")), "spent_hours": hours(daily.get("spent")),
                        "remaining_freebucks": number(daily.get("remaining")), "remaining_hours": hours(daily.get("remaining")),
                        "reset_at": daily.get("resetAt"), "reset_timezone": daily.get("resetTimeZone"),
                    },
                    "wallet": {"balance_freebucks": number(wallet.get("balance")), "balance_hours": hours(wallet.get("balance")), "monthly_bonus": number(wallet.get("monthlyBonus"))},
                    "last_refund": fb.get("lastRefund"), "source": "upstream_session", "status": "available",
                }
            models = self._models()
            subscription = session.get("subscription") or {}
            return {
                "account": account, "balance": balance,
                "subscription_tiers": subscription.get("tiers") or [], "models_count": len(models), "models": models,
                "status": "available" if account is not None and balance is not None else "unknown",
                "source": self._source, "error": deepcopy(self._error),
                "account_status": "available" if account is not None else "unknown",
                "balance_status": "available" if balance is not None else "unknown",
                "metadata": {"scope": "default_credentials_only", "includes_all_accounts": False,
                             "device_registration": "recorded_locally" if self._registration_status == "registered" else self._registration_status,
                             "gateway_content": "text_only"},
            }

    def resolve_model(self, requested: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self.refresh()
            return deepcopy(self._slug_to_row.get((requested or "").strip().lower()))


catalog_service = FreebuffCatalogService()
