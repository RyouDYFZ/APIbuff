"""
Authentication probe and token validation module for Freebuff CLI.
Provides proactive validation against Codebuff / Freebuff API without blocking inference turns.
"""

import os
import sys
import shutil
import json
import time
import subprocess
import logging
from typing import Optional, Dict, Any
import requests

logger = logging.getLogger("freebuff.auth_probe")

_CACHE_RESULT: Optional[Dict[str, Any]] = None
_CACHE_TIMESTAMP: float = 0.0
_CACHE_TTL: float = 60.0  # cache validation results for 60 seconds

def get_stored_token() -> Optional[str]:
    """
    Reads the Freebuff auth token from environment variables (cloud),
    macOS Keychain (service: freebuff-cli), or ~/.config/manicode/credentials.json.
    """
    import shutil
    env_token = os.environ.get("CODEBUFF_API_KEY") or os.environ.get("FREEBUFF_AUTH_TOKEN")
    if env_token:
        return env_token.strip()

    # Query macOS Keychain (only on darwin when security CLI exists)
    if sys.platform == "darwin" and shutil.which("security"):
        try:
            out = subprocess.check_output(
                ["security", "find-generic-password", "-s", "freebuff-cli", "-w"],
                stderr=subprocess.DEVNULL
            )
            token = out.decode().strip()
            if token:
                return token
        except Exception:
            pass

    # Fallback to credentials.json
    creds_path = os.path.expanduser("~/.config/manicode/credentials.json")
    if os.path.exists(creds_path):
        try:
            with open(creds_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                token = data.get("token") or (data.get("default", {}).get("authToken"))
                if token:
                    return token.strip()
        except Exception:
            pass

    return None

def verify_token(force: bool = False) -> Dict[str, Any]:
    """
    Verifies if the current token is authenticated against https://www.codebuff.com/api/v1/me.
    Results are cached for 60 seconds to avoid unnecessary network latency.
    """
    global _CACHE_RESULT, _CACHE_TIMESTAMP

    now = time.time()
    if not force and _CACHE_RESULT is not None and (now - _CACHE_TIMESTAMP < _CACHE_TTL):
        return _CACHE_RESULT

    token = get_stored_token()
    if not token:
        result = {
            "valid": False,
            "status": "missing",
            "message": "No Freebuff credentials found in Keychain. Run ./login.sh to authenticate.",
            "email": None,
            "id": None,
            "login_command": "./login.sh",
            "timestamp": now
        }
        _CACHE_RESULT = result
        _CACHE_TIMESTAMP = now
        return result

    try:
        url = "https://www.codebuff.com/api/v1/me?fields=id,email"
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "freebuff-api-gateway/1.0"
        }
        resp = requests.get(url, headers=headers, timeout=2.5)
        
        if resp.status_code == 200:
            user_data = resp.json()
            result = {
                "valid": True,
                "status": "authenticated",
                "message": "Credentials verified with Freebuff servers.",
                "email": user_data.get("email"),
                "id": user_data.get("id"),
                "login_command": None,
                "timestamp": now
            }
        elif resp.status_code in [401, 403]:
            result = {
                "valid": False,
                "status": "expired",
                "message": f"Token expired or unauthorized (HTTP {resp.status_code}). Run ./login.sh to refresh.",
                "email": None,
                "id": None,
                "login_command": "./login.sh",
                "timestamp": now
            }
        else:
            result = {
                "valid": False,
                "status": "error",
                "message": f"Server returned unexpected status {resp.status_code}.",
                "email": None,
                "id": None,
                "login_command": "./login.sh",
                "timestamp": now
            }
    except Exception as e:
        logger.debug(f"Token verification probe network error: {e}")
        # Network issue or timeout
        result = {
            "valid": False,
            "status": "unreachable",
            "message": f"Could not reach Freebuff authentication server ({e}). Best-effort fallback.",
            "email": None,
            "id": None,
            "login_command": "./login.sh",
            "timestamp": now
        }

    _CACHE_RESULT = result
    _CACHE_TIMESTAMP = now
    return result
