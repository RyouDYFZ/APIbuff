import os
import re
import subprocess
import logging
from typing import Optional, List, Dict, Any
from fastapi import Request
from .models import ChatMessage

logger = logging.getLogger("freebuff.workspace")

# Patterns commonly injected by clients (Cline, Roo Code, Continue, Cursor, Windsurf, Aider)
WORKSPACE_PATTERNS = [
    re.compile(r'(?:Current\s+Working\s+Directory|Working\s+Directory|Workspace\s+Directory|Project\s+Directory|workspace_path|workspace_dir|repo_path|cwd|workspace):\s*[`"\'<]?([/~][^`"\'<>\r\n]+)[`"\'>]?', re.IGNORECASE),
    re.compile(r'(?:in|at)\s+workspace\s+[`"\'<]?([/~][^`"\'<>\r\n]+)[`"\'>]?', re.IGNORECASE),
    re.compile(r'repository\s+root:\s*[`"\'<]?([/~][^`"\'<>\r\n]+)[`"\'>]?', re.IGNORECASE),
]

def resolve_target_workspace(
    http_request: Optional[Request] = None,
    body_workspace: Optional[str] = None,
    messages: Optional[List[ChatMessage]] = None,
    default_fallback: Optional[str] = None
) -> Optional[str]:
    """
    Multi-tier resolution of target workspace:
    1. HTTP headers (X-Workspace-Path, X-Project-Dir, X-Cwd, X-Workspace)
    2. Request body fields (workspace, cwd, project_dir)
    3. Intelligent pattern discovery in system/user messages (Cline/Cursor/Continue/Roo Code prompts)
    4. Default workspace fallback
    """
    # 1. Check HTTP Headers
    if http_request:
        for header_name in ["x-workspace-path", "x-project-dir", "x-cwd", "x-workspace"]:
            val = http_request.headers.get(header_name)
            if val and val.strip():
                candidate = os.path.abspath(os.path.expanduser(val.strip()))
                if os.path.isdir(candidate):
                    logger.info(f"Resolved workspace from HTTP header '{header_name}': {candidate}")
                    return candidate

    # 2. Check Request Body field
    if body_workspace and body_workspace.strip():
        candidate = os.path.abspath(os.path.expanduser(body_workspace.strip()))
        if os.path.isdir(candidate):
            logger.info(f"Resolved workspace from request body: {candidate}")
            return candidate

    # 3. Intelligent extraction from prompt messages (Cline, Roo Code, Continue, Cursor)
    if messages:
        for msg in messages:
            content = msg.content
            if not content or len(content) < 5:
                continue
            for pattern in WORKSPACE_PATTERNS:
                match = pattern.search(content)
                if match:
                    raw_path = match.group(1).strip()
                    candidate = os.path.abspath(os.path.expanduser(raw_path))
                    if os.path.isdir(candidate):
                        logger.info(f"Auto-discovered workspace from client prompt: {candidate}")
                        return candidate

    # 4. Fallback
    if default_fallback and os.path.isdir(default_fallback):
        return default_fallback

    return None

def get_workspace_git_info(workspace_path: str) -> Dict[str, Any]:
    """
    Extracts Git branch, commit count, and status for the workspace.
    """
    if not os.path.isdir(workspace_path):
        return {"exists": False, "is_git": False, "path": workspace_path}

    git_dir = os.path.join(workspace_path, ".git")
    if not os.path.exists(git_dir):
        return {
            "exists": True,
            "is_git": False,
            "path": workspace_path,
            "basename": os.path.basename(workspace_path)
        }

    info: Dict[str, Any] = {
        "exists": True,
        "is_git": True,
        "path": workspace_path,
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
            cwd=workspace_path, stderr=subprocess.DEVNULL
        ).decode().strip()
        info["branch"] = branch
    except Exception:
        pass

    try:
        count = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=workspace_path, stderr=subprocess.DEVNULL
        ).decode().strip()
        info["commit_count"] = int(count)
    except Exception:
        pass

    try:
        diff_out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=workspace_path, stderr=subprocess.DEVNULL
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
            cwd=workspace_path, stderr=subprocess.DEVNULL
        ).decode().strip()
        info["last_commit"] = last_log
    except Exception:
        pass

    try:
        remote = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=workspace_path, stderr=subprocess.DEVNULL
        ).decode().strip()
        info["remote_url"] = remote
    except Exception:
        pass

    return info
