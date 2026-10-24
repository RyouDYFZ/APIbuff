import os
import shutil
from pathlib import Path

# Server settings
HOST = os.getenv("FREEBUFF_API_HOST", "0.0.0.0")
PORT = int(os.getenv("FREEBUFF_API_PORT") or os.getenv("PORT", "8000"))
API_KEY = os.getenv("FREEBUFF_API_KEY") or os.getenv("API_KEY", "")  # Gateway auth

# Upstream Codebuff / Freebuff Cloud Credentials
CODEBUFF_API_KEY = os.getenv("CODEBUFF_API_KEY") or os.getenv("FREEBUFF_AUTH_TOKEN", "")

# Binary and workspace settings
DEFAULT_BIN = os.path.expanduser("~/.config/manicode/freebuff")
FREEBUFF_BIN = os.getenv("FREEBUFF_BIN")
if not FREEBUFF_BIN:
    if os.path.exists(DEFAULT_BIN):
        FREEBUFF_BIN = DEFAULT_BIN
    else:
        FREEBUFF_BIN = shutil.which("freebuff") or DEFAULT_BIN

WORKER_WORKDIR = os.getenv("FREEBUFF_WORKER_WORKDIR", "/tmp/freebuff-api-worker")

# Execution settings
DEFAULT_MODEL = os.getenv("FREEBUFF_DEFAULT_MODEL", "glm-5.3-flash")
TURN_TIMEOUT_SECONDS = int(os.getenv("FREEBUFF_TURN_TIMEOUT", "120"))
SESSION_BOOT_TIMEOUT_SECONDS = int(os.getenv("FREEBUFF_BOOT_TIMEOUT", "20"))
TAKEOVER_AUTO_CONFIRM = os.getenv("FREEBUFF_TAKEOVER_AUTO_CONFIRM", "true").lower() == "true"

# Terminal execution environment settings (Cross-platform Linux & macOS)
WORKER_SPAWN_MODE = os.getenv("FREEBUFF_SPAWN_MODE", "detached_zsh")
USE_LOGIN_SHELL = os.getenv("FREEBUFF_USE_LOGIN_SHELL", "true").lower() == "true"
SHELL_PATH = os.getenv("SHELL") or shutil.which("zsh") or shutil.which("bash") or "/bin/sh"
CUSTOM_TERM_PROGRAM = os.getenv("FREEBUFF_TERM_PROGRAM", "")  # Empty = auto-detect
CUSTOM_TERM_VERSION = os.getenv("FREEBUFF_TERM_PROGRAM_VERSION", "")

# Keystroke Jitter settings (Section III)
JITTER_MODE = os.getenv("FREEBUFF_JITTER_MODE", "human")
JITTER_MIN_MS = float(os.getenv("FREEBUFF_JITTER_MIN_MS", "45.0"))
JITTER_MAX_MS = float(os.getenv("FREEBUFF_JITTER_MAX_MS", "95.0"))
JITTER_DISTRIBUTION = os.getenv("FREEBUFF_JITTER_DISTRIBUTION", "poisson")  # poisson, gaussian, uniform
JITTER_TARGET_MAX_CPS = float(os.getenv("FREEBUFF_JITTER_MAX_CPS", "11.5"))
PRE_ENTER_MIN_MS = float(os.getenv("FREEBUFF_PRE_ENTER_MIN_MS", "250.0"))
PRE_ENTER_MAX_MS = float(os.getenv("FREEBUFF_PRE_ENTER_MAX_MS", "500.0"))
