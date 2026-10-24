import os
import re
import subprocess
import logging
from typing import Optional, List, Dict, Any
from fastapi import Request
from .models import ChatMessage
from .exceptions import FreebuffException, FreebuffPermissionError

logger = logging.getLogger("freebuff.workspace")

# Controls metadata redaction. Workspace allowlist enforcement is always enabled.
STRICT_ISOLATION = os.getenv("FREEBUFF_STRICT_ISOLATION", "false").lower() == "true"
GIT_COMMAND_TIMEOUT_SECONDS = 5

# Patterns commonly injected by clients (Cline, Roo Code, Continue, Cursor, Windsurf, Aider)
WORKSPACE_PATTERNS = [
    re.compile(r'(?:Current\s+Working\s+Directory|Working\s+Directory|Workspace\s+Directory|Project\s+Directory|workspace_path|workspace_dir|repo_path|cwd|workspace):\s*[`"\'<]?([/~][^`"\'<>\r\n]+)[`"\'>]?', re.IGNORECASE),
    re.compile(r'(?:in|at)\s+workspace\s+[`"\'<]?([/~][^`"\'<>\r\n]+)[`"\'>]?', re.IGNORECASE),
    re.compile(r'repository\s+root:\s*[`"\'<]?([/~][^`"\'<>\r\n]+)[`"\'>]?', re.IGNORECASE),
]

def sanitize_host_paths(text: str, replacement_root: str = "/workspace") -> str:
    """
    Scrubs host user paths (e.g. /Users/<username>/..., /home/<username>/..., C:\\Users\\<username>\\...)
    from text to prevent client prompts (Cursor, Cline, Continue) from leaking personal
    identities, system usernames, or host repository locations to the upstream LLM/CLI.
    """
    if not text:
        return text
    # Match /Users/<username> or /home/<username>
    scrubbed = re.sub(r'/(?:Users|home)/[a-zA-Z0-9_\-\.]+([/\s\'"`])', f'{replacement_root}\\1', text)
    scrubbed = re.sub(r'/(?:Users|home)/[a-zA-Z0-9_\-\.]+$', replacement_root, scrubbed)
    # Match Windows C:\Users\<username>
    scrubbed = re.sub(r'[a-zA-Z]:\\Users\\[a-zA-Z0-9_\-\.]+', replacement_root, scrubbed)
    return scrubbed

def allowed_workspace_roots() -> List[str]:
    """Return operator-configured roots, resolved through symlinks before comparison."""
    roots = [os.getenv("FREEBUFF_WORKER_WORKDIR", "/tmp/freebuff-api-worker")]
    roots.extend(os.getenv("FREEBUFF_ALLOWED_WORKSPACE_ROOTS", "").split(os.pathsep))
    return [os.path.realpath(os.path.expanduser(root.strip())) for root in roots if root.strip()]


def is_safe_isolated_path(path: str) -> bool:
    """Accept existing directories only within a configured, real filesystem root.

    Names such as ``sandbox`` or ``worker`` grant no authority. This is a path
    selection boundary, not an OS filesystem sandbox for the CLI process.
    """
    if not isinstance(path, str) or not path.strip() or "\0" in path:
        return False
    try:
        normalized = os.path.realpath(os.path.expanduser(path))
        if not os.path.isdir(normalized):
            return False
        return any(os.path.commonpath((root, normalized)) == root for root in allowed_workspace_roots())
    except (OSError, ValueError):
        return False


def validate_workspace_path(path: str) -> str:
    """Validate an explicit selection without silently selecting another directory."""
    if not isinstance(path, str) or not path.strip() or "\0" in path:
        raise FreebuffException(
            "Workspace must be a nonempty directory path.", status_code=400,
            error_type="invalid_request_error", error_code="invalid_workspace",
        )
    try:
        normalized = os.path.realpath(os.path.expanduser(path.strip()))
        exists = os.path.isdir(normalized)
    except (OSError, ValueError):
        exists = False
    if not exists:
        raise FreebuffException(
            "Workspace directory does not exist.", status_code=400,
            error_type="invalid_request_error", error_code="invalid_workspace",
        )
    if not is_safe_isolated_path(normalized):
        raise FreebuffPermissionError(
            "Workspace is outside the configured allowed roots.", error_code="workspace_not_allowed",
        )
    return normalized

def resolve_target_workspace(
    http_request: Optional[Request] = None,
    body_workspace: Optional[str] = None,
    messages: Optional[List[ChatMessage]] = None,
    default_fallback: Optional[str] = None
) -> Optional[str]:
    """
    Resolve an authorized selection, leaving account defaults to the worker pool:
    1. HTTP headers (X-Workspace-Path, X-Project-Dir, X-Cwd, X-Workspace)
    2. Request body fields (workspace, cwd, project_dir)
    3. Pattern discovery in system/developer messages
    4. An explicitly supplied, validated default (otherwise None)
    """
    # 1. Check HTTP Headers
    if http_request:
        for header_name in ["x-workspace-path", "x-project-dir", "x-cwd", "x-workspace"]:
            val = http_request.headers.get(header_name)
            if val is not None:
                return validate_workspace_path(val)

    # 2. Check Request Body field
    if body_workspace is not None:
        return validate_workspace_path(body_workspace)

    # 3. Intelligent extraction from prompt messages (Cline, Roo Code, Continue, Cursor)
    if messages:
        for msg in messages:
            if msg.role not in ("system", "developer"):
                continue
            content = msg.content
            if not content or len(content) < 5:
                continue
            for pattern in WORKSPACE_PATTERNS:
                match = pattern.search(content)
                if match:
                    return validate_workspace_path(match.group(1).strip())

    if default_fallback is not None:
        return validate_workspace_path(default_fallback)
    return None

def get_workspace_git_info(workspace_path: str) -> Dict[str, Any]:
    """
    Extracts Git branch, commit count, and status for the workspace.
    In strict isolation mode, scrubs all remote URLs and personal author names.
    """
    if not os.path.isdir(workspace_path):
        return {"exists": False, "is_git": False, "path": sanitize_host_paths(workspace_path)}

    git_dir = os.path.join(workspace_path, ".git")
    if not os.path.exists(git_dir):
        return {
            "exists": True,
            "is_git": False,
            "path": sanitize_host_paths(workspace_path),
            "basename": os.path.basename(workspace_path)
        }

    info: Dict[str, Any] = {
        "exists": True,
        "is_git": True,
        "path": sanitize_host_paths(workspace_path),
        "basename": os.path.basename(workspace_path),
        "branch": None,
        "commit_count": 0,
        "clean": True,
        "modified_files": 0,
        "last_commit": None,
        "remote_url": None
    }

    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=workspace_path, stderr=subprocess.DEVNULL, timeout=GIT_COMMAND_TIMEOUT_SECONDS
        ).decode().strip()
        info["branch"] = branch
    except Exception:
        pass

    try:
        count = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=workspace_path, stderr=subprocess.DEVNULL, timeout=GIT_COMMAND_TIMEOUT_SECONDS
        ).decode().strip()
        info["commit_count"] = int(count)
    except Exception:
        pass

    try:
        diff_out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=workspace_path, stderr=subprocess.DEVNULL, timeout=GIT_COMMAND_TIMEOUT_SECONDS
        ).decode().strip()
        if diff_out:
            lines = [l for l in diff_out.splitlines() if l.strip()]
            info["clean"] = len(lines) == 0
            info["modified_files"] = len(lines)
    except Exception:
        pass

    try:
        last_log = subprocess.check_output(
            ["git", "log", "-1", "--format=%h %s (%an, %cr)"],
            cwd=workspace_path, stderr=subprocess.DEVNULL, timeout=GIT_COMMAND_TIMEOUT_SECONDS
        ).decode().strip()
        if STRICT_ISOLATION:
            # Redact personal author name to "Cloud Developer"
            last_log = re.sub(r'\([^,]+,', '(Cloud Developer,', last_log)
        info["last_commit"] = last_log
    except Exception:
        pass

    try:
        if not STRICT_ISOLATION:
            remote = subprocess.check_output(
                ["git", "remote", "get-url", "origin"],
                cwd=workspace_path, stderr=subprocess.DEVNULL, timeout=GIT_COMMAND_TIMEOUT_SECONDS
            ).decode().strip()
            info["remote_url"] = remote
        else:
            # Completely block remote URL leakage in strict isolation
            info["remote_url"] = None
    except Exception:
        pass

    return info
