import sys
import os
import time
import json
import urllib.request
import urllib.error

from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_URL = "http://127.0.0.1:8000"
REAL_REPO_PATH = os.environ.get(
    "REAL_REPO_PATH",
    str(Path(__file__).resolve().parents[1])
)

def test_get_current_workspace():
    print("[1/4] Testing GET /v1/workspace ...")
    req = urllib.request.Request(f"{BASE_URL}/v1/workspace")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        print(f"  -> Active workspace: {data.get('active_workspace')}")
        print(f"  -> Git info: {data.get('git')}")
        assert "active_workspace" in data
        assert "git" in data
    print("  [PASS] GET /v1/workspace passed!\n")

def test_switch_workspace_dynamically():
    print(f"[2/4] Testing POST /v1/workspace to '{REAL_REPO_PATH}' ...")
    payload = {"path": REAL_REPO_PATH}
    req = urllib.request.Request(
        f"{BASE_URL}/v1/workspace",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        print(f"  -> Switch status: {data.get('status')}")
        print(f"  -> Active workspace: {data.get('active_workspace')}")
        git = data.get("git", {})
        print(f"  -> Real Git Branch: {git.get('branch')}")
        print(f"  -> Real Git Commits: {git.get('commit_count')}")
        print(f"  -> Real Last Commit: {git.get('last_commit')}")
        assert data.get("active_workspace") == REAL_REPO_PATH
        assert git.get("is_git") is True
        assert git.get("branch") is not None
        assert git.get("commit_count", 0) >= 1
    print("  [PASS] Dynamic workspace switch & real Git binding verified!\n")

def test_workspace_header_resolution():
    print("[3/4] Testing workspace resolution via HTTP header 'X-Workspace-Path' ...")
    from app.workspace import resolve_target_workspace
    class DummyRequest:
        def __init__(self, headers):
            self.headers = headers

    req = DummyRequest({"x-workspace-path": REAL_REPO_PATH})
    resolved = resolve_target_workspace(http_request=req)
    print(f"  -> Resolved from header: {resolved}")
    assert resolved == REAL_REPO_PATH
    print("  [PASS] X-Workspace-Path header resolution verified!\n")

def test_prompt_auto_discovery():
    print("[4/4] Testing prompt auto-discovery (Cline / Cursor / Roo Code system prompt) ...")
    from app.workspace import resolve_target_workspace
    from app.models import ChatMessage

    sample_prompt = f"""You are a helpful coding assistant.
Current Working Directory: {REAL_REPO_PATH}
Please inspect the files in this repository."""

    messages = [
        ChatMessage(role="system", content=sample_prompt),
        ChatMessage(role="user", content="Hello")
    ]
    resolved = resolve_target_workspace(messages=messages)
    print(f"  -> Auto-discovered from prompt: {resolved}")
    assert resolved == REAL_REPO_PATH
    print("  [PASS] Client system prompt auto-discovery verified!\n")

if __name__ == "__main__":
    test_get_current_workspace()
    test_switch_workspace_dynamically()
    test_workspace_header_resolution()
    test_prompt_auto_discovery()
    print("ALL WORKSPACE MAPPING TESTS PASSED SUCCESSFULLY!")
