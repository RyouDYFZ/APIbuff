from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
import os
from ..terminal_env import audit_runtime_environment, get_standard_terminal_env
from ..jitter import KeystrokeJitterSimulator, JitterConfig
from ..pool import global_pool
from ..config import WORKER_WORKDIR
from ..workspace import get_workspace_git_info

router = APIRouter()

class SetWorkspaceRequest(BaseModel):
    path: str

class JitterTestRequest(BaseModel):
    text: str = "Write a python function to compute fibonacci numbers."
    mode: str = "human"
    distribution: str = "poisson"
    min_delay_ms: float = 45.0
    max_delay_ms: float = 95.0
    target_max_cps: float = 11.5
    pre_enter_min_ms: float = 250.0
    pre_enter_max_ms: float = 500.0

@router.get("/v1/system/env-audit")
@router.get("/system/env-audit")
async def get_environment_audit():
    """
    Audits the execution environment and process tree of the Freebuff CLI worker.
    Verifies that p=python is eliminated and p=shell (zsh -l) is active.
    """
    worker_pid = global_pool.worker.get_freebuff_pid() if (global_pool.worker and global_pool.worker.is_running) else None
    audit = audit_runtime_environment(worker_pid)
    audit["worker_running"] = global_pool.worker.is_running if global_pool.worker else False
    audit["worker_ready"] = global_pool.worker.ready_event.is_set() if global_pool.worker else False
    return audit

@router.post("/v1/system/jitter-test")
@router.post("/system/jitter-test")
async def test_keystroke_jitter(req: JitterTestRequest):
    """
    Simulates keystroke jitter typing for testing and validation.
    Returns character intervals, sliding window CPS, composeMs, and the Freebuff wire profile.
    """
    config = JitterConfig(
        mode=req.mode,
        min_delay_ms=req.min_delay_ms,
        max_delay_ms=req.max_delay_ms,
        distribution=req.distribution,
        target_max_cps=req.target_max_cps,
        pre_enter_min_ms=req.pre_enter_min_ms,
        pre_enter_max_ms=req.pre_enter_max_ms
    )
    simulator = KeystrokeJitterSimulator(config)
    
    # Run test using pipe to record physical timing
    import os
    r_fd, w_fd = os.pipe()
    try:
        metrics = await simulator.type_into_fd(w_fd, req.text, append_return=True)
        read_bytes = os.read(r_fd, 65536)
    finally:
        os.close(r_fd)
        os.close(w_fd)

    res = metrics.to_dict()
    res["verified_cps_under_12"] = metrics.max_cps <= 12
    res["captured_pipe_bytes_len"] = len(read_bytes)
    return res

@router.post("/v1/system/paste-test")
@router.post("/system/paste-test")
async def test_bracketed_paste(req: JitterTestRequest):
    """
    Tests bracketed paste emulation (\x1b[200~ ... \x1b[201~) for code and multiline inputs.
    """
    simulator = KeystrokeJitterSimulator()
    import os
    r_fd, w_fd = os.pipe()
    try:
        metrics = await simulator.paste_into_fd(w_fd, req.text, append_return=True)
        read_bytes = os.read(r_fd, 65536)
    finally:
        os.close(r_fd)
        os.close(w_fd)

    res = metrics.to_dict()
    res["contains_bracketed_paste_seq"] = b"\x1b[200~" in read_bytes and b"\x1b[201~" in read_bytes
    res["captured_pipe_bytes_len"] = len(read_bytes)
    return res

@router.get("/v1/system/jitter-metrics")
@router.get("/system/jitter-metrics")
async def get_latest_jitter_metrics():
    """
    Returns the keystroke timing and CPS metrics of the most recent real turn executed by the worker.
    """
    if not global_pool.worker or not global_pool.worker.last_jitter_metrics:
        return {"status": "no_turns_executed_yet"}
    return global_pool.worker.last_jitter_metrics.to_dict()

@router.get("/v1/workspace")
@router.get("/workspace")
async def get_active_workspace():
    """
    Returns the current active workspace of the Freebuff worker along with its Git repository metadata.
    """
    cur_workdir = global_pool.worker.workdir if global_pool.worker else WORKER_WORKDIR
    git_info = get_workspace_git_info(cur_workdir)
    return {
        "active_workspace": cur_workdir,
        "basename": os.path.basename(cur_workdir),
        "git": git_info
    }

@router.post("/v1/workspace")
@router.post("/workspace")
async def set_active_workspace(req: SetWorkspaceRequest):
    """
    Dynamically switches the active workspace of the Freebuff worker.
    Restarts the worker inside the new workspace so Freebuff binds to the real project Git context.
    """
    target = os.path.abspath(os.path.expanduser(req.path))
    if not os.path.isdir(target):
        raise HTTPException(status_code=400, detail=f"Directory does not exist: {target}")

    if global_pool.worker is None:
        await global_pool.initialize()

    await global_pool.worker.ensure_workspace(target)
    git_info = get_workspace_git_info(target)
    return {
        "status": "switched",
        "active_workspace": target,
        "basename": os.path.basename(target),
        "git": git_info
    }
