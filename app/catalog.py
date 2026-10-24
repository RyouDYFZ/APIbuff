import os
import sys
import json
import time
import base64
import hashlib
import re
import subprocess
import logging
from typing import Dict, Any, List, Optional
import requests
try:
    from cryptography.hazmat.primitives.serialization import load_der_private_key
except ImportError:
    load_der_private_key = None

logger = logging.getLogger("freebuff.catalog")

FREEBUFF_PER_HOUR = 5

# Built-in fallback catalog containing verified Freebuff CLI models and default pricing
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

class FreebuffCatalogService:
    def __init__(self, config_dir: str = "~/.config/manicode"):
        self.config_dir = os.path.expanduser(config_dir)
        self.key_path = os.path.join(self.config_dir, "device-key.json")
        self.token: Optional[str] = None
        self.key_id: Optional[str] = None
        self.priv_key = None
        
        self._cached_catalog: Dict[str, Any] = DEFAULT_FALLBACK_CATALOG
        self._cached_session: Optional[Dict[str, Any]] = None
        self._cache_time: float = 0
        self._cache_ttl: float = 60.0  # seconds
        
        self._slug_to_row: Dict[str, Dict[str, Any]] = {}
        self._key_to_row: Dict[str, Dict[str, Any]] = {}

        self._load_credentials()
        self._rebuild_index()

    def _load_credentials(self):
        import shutil
        token = os.environ.get("CODEBUFF_API_KEY") or os.environ.get("FREEBUFF_AUTH_TOKEN")
        if not token and sys.platform == "darwin" and shutil.which("security"):
            try:
                out = subprocess.check_output(
                    ["security", "find-generic-password", "-s", "freebuff-cli", "-w"],
                    stderr=subprocess.DEVNULL
                )
                token = out.decode().strip()
            except Exception:
                pass
        
        if not token:
            creds_path = os.path.join(self.config_dir, "credentials.json")
            if os.path.exists(creds_path):
                try:
                    with open(creds_path, "r") as f:
                        cdata = json.load(f)
                        token = cdata.get("token") or cdata.get("default", {}).get("authToken")
                except Exception:
                    pass
        self.token = token

        if load_der_private_key and os.path.exists(self.key_path):
            try:
                with open(self.key_path, "r") as f:
                    kdata = json.load(f)
                    regs = kdata.get("registrations", {})
                    if regs:
                        self.key_id = list(regs.values())[0]
                    priv_b64 = kdata.get("privateKey")
                    if priv_b64:
                        priv_der = b64u_decode(priv_b64)
                        self.priv_key = load_der_private_key(priv_der, password=None)
            except Exception as e:
                logger.warning(f"Failed to load device key: {e}")

    def _sign_headers(self, method: str, path: str, body: bytes = b'', fetch_id: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "x-freebuff-catalog-protocol": "1",
            "x-freebuff-client": "cli",
            "x-fb-timezone": "Asia/Shanghai",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        if self.priv_key and self.key_id:
            ts = int(time.time() * 1000)
            body_sha = hashlib.sha256(body).hexdigest()
            payload = f"freebuff-device-v1\n{method.upper()}\n{path}\n{ts}\n{body_sha}\n{fetch_id or ''}".encode("utf-8")
            sig = self.priv_key.sign(payload)
            headers.update({
                "x-freebuff-device-key": self.key_id,
                "x-freebuff-device-ts": str(ts),
                "x-freebuff-device-sig": b64u_encode(sig),
            })
        return headers

    def refresh(self, force: bool = False):
        now = time.time()
        if not force and (now - self._cache_time < self._cache_ttl):
            return

        try:
            h_cat = self._sign_headers("GET", "/api/v1/freebuff/models")
            r_cat = requests.get("https://www.codebuff.com/api/v1/freebuff/models", headers=h_cat, timeout=3.5)
            if r_cat.status_code == 200:
                self._cached_catalog = r_cat.json()

            h_sess = self._sign_headers("GET", "/api/v1/freebuff/session")
            r_sess = requests.get("https://www.codebuff.com/api/v1/freebuff/session", headers=h_sess, timeout=3.5)
            if r_sess.status_code == 200:
                self._cached_session = r_sess.json()

            self._cache_time = now
            self._rebuild_index()
        except Exception as e:
            logger.debug(f"Catalog online refresh skipped ({e}). Operating on local fallback catalog.")
            self._cache_time = now
            self._rebuild_index()

    def _rebuild_index(self):
        catalog = self._cached_catalog or DEFAULT_FALLBACK_CATALOG
        if "rows" not in catalog:
            return

        prices = (self._cached_session or {}).get("freebucks", {}).get("prices", {})
        notices = (self._cached_session or {}).get("freebucks", {}).get("priceNotices", {})
        plan_req = set((self._cached_session or {}).get("freebucks", {}).get("planRequiredModelIds", []))
        off_peak = (self._cached_session or {}).get("freebucks", {}).get("offPeak", {})

        slug_map = {}
        key_map = {}

        # Expanded known aliases and universal multi-client bindings
        KNOWN_ALIASES = {
            "m-7e20df6765": ["glm-5.3-flash", "glm-5.3", "glm", "glm-4", "glm-5", "z-ai/glm-5.3-flash", "default", "freebuff-default"],
            "m-00032eaeec": ["mimo-v2.6-flash-fast", "mimo-v2.6-flash", "mimo-flash", "mimo", "mimo-fast"],
            "m-096e75164d": ["deepseek-v4.1-flash", "deepseek-v4-flash", "deepseek-flash", "deepseek", "deepseek-chat", "deepseek-reasoner", "deepseek-coder"],
            "m-127314bb39": ["deepseek-v4.1-flash-fast", "deepseek-fast"],
            "m-cb71f819fe": ["mimo-2.6-pro", "mimo-pro"],
            "m-5a5d0e255e": ["gpt-6-luna", "gpt-6", "luna", "openai/gpt-6-luna", "gpt-4o", "gpt-4", "gpt-4-turbo", "o1", "o3-mini"],
            "m-6adcd6d671": ["gpt-6.1-sol", "gpt-6.1", "sol", "openai/gpt-6.1-sol", "gpt-4o-mini", "o3"],
            "m-94d83cc7e6": ["gemini-4-argon", "google/gemini-4-argon", "gemini-1.5-pro", "gemini-2.5", "gemini-pro"],
            "m-5dafce7f08": ["gemini-3.8-flash", "google/gemini-3.8-flash", "gemini-2.0-flash", "gemini-flash", "gemini-1.5-flash"],
            "m-79293c5b7d": ["claude-haiku-5.5", "claude-haiku", "claude-3-5-sonnet", "claude-3-7-sonnet", "claude-3-haiku", "claude-sonnet"],
            "m-5b6a604eb6": ["claude-haiku-5.5-100k"],
            "m-9a7e098cc1": ["solar-pro-4", "solar-pro", "solar"],
            "m-69307952f8": ["solar-mini-4", "solar-mini"],
            "m-ddc687c50f": ["nemotron-3.5-lightning", "nemotron"],
            "m-e0ae3cb3e2": ["gemini-3.8-pro-preview", "gemini-3.8-pro"],
            "m-6d0ac71639": ["glm-5.5"],
            "m-8d40fa364e": ["deepseek-v5-preview", "deepseek-v5"]
        }

        for row in catalog["rows"]:
            key = row["key"]
            display_name = row["displayName"]
            clean_slug = slugify(display_name)
            primary_slug = KNOWN_ALIASES.get(key, [clean_slug])[0]
            
            p_freebucks = prices.get(key, row.get("defaultPrice", 0))
            p_hours = (p_freebucks / FREEBUFF_PER_HOUR) if p_freebucks is not None else None
            is_free = (p_freebucks == 0)

            all_aliases = list(set([clean_slug, key, primary_slug] + KNOWN_ALIASES.get(key, [])))

            enriched = {
                "key": key,
                "handle": row["handle"],
                "displayName": display_name,
                "tagline": row.get("tagline", ""),
                "primarySlug": primary_slug,
                "aliases": all_aliases,
                "access": row.get("access", "open"),
                "planRequired": (key in plan_req) or (row.get("access") == "locked"),
                "premium": row.get("premium", False),
                "dataUse": row.get("dataUse", "service"),
                "multimodal": row.get("multimodal", False),
                "contextWindow": row.get("contextWindow", 128000),
                "efforts": row.get("efforts", []),
                "defaultEffort": row.get("defaultEffort"),
                "badges": row.get("badges", []),
                "pricing": {
                    "freebucks_per_hour": p_freebucks,
                    "hours_equivalent": p_hours,
                    "is_free": is_free,
                    "notice": notices.get(key),
                    "off_peak": off_peak.get(key),
                }
            }

            key_map[key] = enriched
            for alias in all_aliases:
                slug_map[alias.lower()] = enriched

        self._key_to_row = key_map
        self._slug_to_row = slug_map

    def get_all_models(self) -> List[Dict[str, Any]]:
        self.refresh()
        res = []
        for key, row in self._key_to_row.items():
            res.append({
                "id": row["primarySlug"],
                "object": "model",
                "created": 1700000000,
                "owned_by": "freebuff",
                "key": key,
                "display_name": row["displayName"],
                "tagline": row["tagline"],
                "aliases": row["aliases"],
                "pricing": row["pricing"],
                "capabilities": {
                    "context_window": row["contextWindow"],
                    "multimodal": row["multimodal"],
                    "efforts": row["efforts"],
                    "default_effort": row["defaultEffort"],
                    "access": row["access"],
                    "plan_required": row["planRequired"],
                    "premium": row["premium"],
                    "data_use": row["dataUse"],
                    "badges": [b.get("label") for b in row["badges"]]
                }
            })
        return sorted(res, key=lambda m: (m["pricing"]["freebucks_per_hour"] if m["pricing"]["freebucks_per_hour"] is not None else 999))

    def get_pricing_overview(self) -> Dict[str, Any]:
        self.refresh()
        fb = (self._cached_session or {}).get("freebucks", {})
        sub = (self._cached_session or {}).get("subscription", {})
        models = self.get_all_models()

        total_fb = fb.get("balance", 0)
        daily = fb.get("daily", {})
        wallet = fb.get("wallet", {})

        return {
            "account": {
                "access_tier": (self._cached_session or {}).get("accessTier", "free_mode"),
                "country_code": (self._cached_session or {}).get("countryCode", "US"),
                "country_verified": (self._cached_session or {}).get("countryVerified", True)
            },
            "balance": {
                "total_freebucks": total_fb,
                "total_hours": round(total_fb / FREEBUFF_PER_HOUR, 2),
                "daily": {
                    "limit_freebucks": daily.get("limit", 25),
                    "limit_hours": round(daily.get("limit", 25) / FREEBUFF_PER_HOUR, 2),
                    "spent_freebucks": daily.get("spent", 0),
                    "spent_hours": round(daily.get("spent", 0) / FREEBUFF_PER_HOUR, 2),
                    "remaining_freebucks": daily.get("remaining", 25),
                    "remaining_hours": round(daily.get("remaining", 25) / FREEBUFF_PER_HOUR, 2),
                    "reset_at": daily.get("resetAt"),
                    "reset_timezone": daily.get("resetTimeZone", "Asia/Shanghai")
                },
                "wallet": {
                    "balance_freebucks": wallet.get("balance", 0),
                    "balance_hours": round(wallet.get("balance", 0) / FREEBUFF_PER_HOUR, 2),
                    "monthly_bonus": wallet.get("monthlyBonus", 0)
                },
                "last_refund": fb.get("lastRefund")
            },
            "subscription_tiers": sub.get("tiers", []),
            "models_count": len(models),
            "models": models
        }

    def resolve_model(self, requested: str) -> Optional[Dict[str, Any]]:
        self.refresh()
        req = (requested or "").strip().lower()
        if req in self._slug_to_row:
            return self._slug_to_row[req]
        if req in self._key_to_row:
            return self._key_to_row[req]
        for slug, row in self._slug_to_row.items():
            if req in slug or slug in req:
                return row
        return None

# Singleton instance
catalog_service = FreebuffCatalogService()
