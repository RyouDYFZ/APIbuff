"""Read and seed the official CLI's on-disk credential format."""

import json
import os
import tempfile
from typing import Optional


def account_config_dir(home_dir: str) -> str:
    """Return the account's absolute config path, independent of the host env."""
    return os.path.join(os.path.abspath(os.path.expanduser(home_dir)), ".config", "manicode")


def default_config_dir() -> str:
    """Resolve the native CLI's configured directory for single-account login."""
    configured = os.environ.get("FREEBUFF_CONFIG_DIR")
    if configured:
        if not os.path.isabs(configured):
            raise ValueError("FREEBUFF_CONFIG_DIR must be an absolute path")
        return configured
    return account_config_dir(os.path.expanduser("~"))


def read_cli_auth_token(config_dir: str) -> Optional[str]:
    """Read a file token; a keychain pointer is not a portable credential."""
    try:
        with open(os.path.join(config_dir, "credentials.json"), encoding="utf-8") as f:
            data = json.load(f)
        profile = data.get("default")
        token = profile.get("authToken") if isinstance(profile, dict) else None
        # Accept the gateway's old flat format so an existing sandbox can migrate.
        token = token or data.get("accessToken") or data.get("token")
        if isinstance(token, str) and token.strip() and token != "unauthenticated":
            return token.strip()
    except (OSError, ValueError, AttributeError):
        pass
    return None


def write_cli_credentials(
    config_dir: str, token: Optional[str], email: Optional[str] = None
) -> Optional[str]:
    """Atomically seed default.authToken without replacing profile metadata.

    The CLI accepts an inline authToken with no tokenStore field, or a
    tokenStore='keychain' pointer. It does not accept tokenStore='file'.
    A missing token leaves existing login state untouched.
    """
    if not token or not token.strip() or token.strip() == "unauthenticated":
        return None
    config_dir = os.path.abspath(config_dir)
    if os.path.islink(config_dir):
        raise ValueError("Refusing to write credentials through a config directory symlink")
    os.makedirs(config_dir, mode=0o700, exist_ok=True)
    os.chmod(config_dir, 0o700)
    credentials_path = os.path.join(config_dir, "credentials.json")
    if os.path.islink(credentials_path):
        raise ValueError("Refusing to overwrite a credentials symlink")

    try:
        with open(credentials_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("Existing credentials must contain a JSON object")
    except FileNotFoundError:
        data = {}

    profile = data.get("default")
    profile = dict(profile) if isinstance(profile, dict) else {}
    profile.pop("tokenStore", None)
    profile["authToken"] = token.strip()
    if email is not None:
        profile["email"] = email
    elif not isinstance(profile.get("email"), str):
        profile["email"] = ""
    if not isinstance(profile.get("name"), str):
        profile["name"] = ""
    data["default"] = profile
    # Remove the obsolete flat gateway fields, including the stale token.
    data.pop("accessToken", None)
    data.pop("tokenStore", None)

    fd, temp_path = tempfile.mkstemp(prefix=".credentials-", suffix=".tmp", dir=config_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, credentials_path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return credentials_path

