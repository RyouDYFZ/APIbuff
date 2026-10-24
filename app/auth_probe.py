"""Explicit, scoped credential diagnostics; public health never calls this module."""
import hashlib
import json
import os
import threading
import time
from copy import deepcopy
from typing import Any, Dict, Optional

import requests

from .credentials import default_config_dir

API_HOST = "https://www.codebuff.com"
_CACHE_RESULT: Optional[Dict[str, Any]] = None
_CACHE_TIMESTAMP = 0.0
_CACHE_SCOPE = None
_CACHE_TTL = 60.0
_CACHE_LOCK = threading.RLock()


def _token(value):
    return value.strip() if isinstance(value, str) and value.strip() not in ("", "unauthenticated") else None


def read_auth_context(config_dir: Optional[str] = None) -> Dict[str, Any]:
    """Use only this profile's inline token or an explicitly supplied env token.

    A keychain pointer is intentionally unresolved. Reading a generic keychain
    service can select a different profile, and can prompt on a headless server.
    """
    directory = os.path.abspath(os.path.expanduser(config_dir or default_config_dir()))
    data = {}
    try:
        with open(os.path.join(directory, "credentials.json"), encoding="utf-8") as handle:
            loaded = json.load(handle)
            if isinstance(loaded, dict):
                data = loaded
    except (OSError, ValueError):
        pass
    profile = data.get("default")
    profile = profile if isinstance(profile, dict) else {}
    file_token = _token(profile.get("authToken"))
    if not profile:
        file_token = _token(data.get("accessToken")) or _token(data.get("token"))
    env_token = _token(os.environ.get("CODEBUFF_API_KEY")) or _token(os.environ.get("FREEBUFF_AUTH_TOKEN"))
    token = env_token or file_token
    # Match the CLI's accountFor(): a stored ID belongs only to that same token.
    account_id = profile.get("id") if token and token == file_token else None
    if not isinstance(account_id, str) or not account_id:
        account_id = None
    return {
        "config_dir": directory,
        "token": token,
        "account_id": account_id,
        "source": "environment" if env_token else "credentials_file" if file_token else "keychain_unavailable" if profile.get("tokenStore") == "keychain" else "missing",
    }


def get_stored_token(config_dir: Optional[str] = None) -> Optional[str]:
    return read_auth_context(config_dir)["token"]


def verify_token(force: bool = False) -> Dict[str, Any]:
    """Opt-in /me diagnostic, cached only for the same directory and token."""
    global _CACHE_RESULT, _CACHE_TIMESTAMP, _CACHE_SCOPE
    with _CACHE_LOCK:
        context = read_auth_context()
        token = context["token"]
        scope = (context["config_dir"], hashlib.sha256(token.encode()).hexdigest() if token else None)
        now = time.time()
        if not force and scope == _CACHE_SCOPE and _CACHE_RESULT is not None and now - _CACHE_TIMESTAMP < _CACHE_TTL:
            return deepcopy(_CACHE_RESULT)
        result = {
            "valid": None,
            "status": "unknown",
            "message": "Credential validity has not been established.",
            "email": None,
            "id": None,
            "login_command": None,
            "timestamp": now,
            "source": context["source"],
            "scope": "default_credentials_only",
        }
        if not token:
            result.update(
                status="unknown" if context["source"] == "keychain_unavailable" else "missing",
                message="The configured profile has no readable inline token; keychain pointers are not resolved by this gateway.",
            )
        else:
            try:
                response = requests.get(
                    API_HOST + "/api/v1/me?fields=id,email",
                    headers={"Authorization": f"Bearer {token}", "User-Agent": "freebuff-api-gateway/1.0"},
                    timeout=2.5,
                )
                result["http_status"] = response.status_code
                if response.status_code == 200:
                    user = response.json()
                    if isinstance(user, dict) and isinstance(user.get("id"), str) and user["id"]:
                        result.update(valid=True, status="authenticated", message="The explicit identity diagnostic accepted these credentials.", email=user.get("email"), id=user["id"])
                    else:
                        result["message"] = "The identity endpoint returned an unrecognized response; credential validity is unknown."
                elif response.status_code == 401:
                    result.update(valid=False, status="unauthorized", message="The identity diagnostic rejected these credentials (HTTP 401).")
                elif response.status_code == 403:
                    result.update(status="forbidden", message="The identity diagnostic was denied (HTTP 403); this does not establish token expiry.")
                else:
                    result["message"] = f"The identity diagnostic returned HTTP {response.status_code}; credential validity is unknown."
            except Exception:
                result.update(status="unreachable", message="The identity diagnostic could not be completed; credential validity is unknown.")
        _CACHE_RESULT, _CACHE_TIMESTAMP, _CACHE_SCOPE = deepcopy(result), now, scope
        return result
