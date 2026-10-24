"""Bounded local input diagnostics and coordinated workspace administration."""

import asyncio
import os
import tempfile
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator

from ..config import WORKER_WORKDIR
from ..exceptions import FreebuffException
from ..jitter import JitterConfig, KeystrokeJitterSimulator, MAX_DELAY_MS, MAX_TARGET_CPS
from ..pool import global_pool
from ..terminal_env import audit_runtime_environment
from ..workspace import get_workspace_git_info

router = APIRouter()
DIAGNOSTIC_TIMEOUT_SECONDS = 5.0
MAX_DIAGNOSTIC_CHARS = 4096
MAX_HUMAN_DIAGNOSTIC_CHARS = 128


class SetWorkspaceRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class DiagnosticInputRequest(BaseModel):
    model_config = {"extra": "forbid", "allow_inf_nan": False}
    text: str = Field(default="Local typing sample.", max_length=MAX_DIAGNOSTIC_CHARS)
    mode: Literal["human", "fast", "instant"] = "human"
    distribution: Literal["poisson", "gaussian", "uniform"] = "poisson"
    min_delay_ms: float = Field(default=45.0, ge=0, le=MAX_DELAY_MS, strict=True)
    max_delay_ms: float = Field(default=95.0, ge=0, le=MAX_DELAY_MS, strict=True)
    target_max_cps: float = Field(default=11.5, ge=1, le=MAX_TARGET_CPS, strict=True)
    pre_enter_min_ms: float = Field(default=250.0, ge=0, le=MAX_DELAY_MS, strict=True)
    pre_enter_max_ms: float = Field(default=500.0, ge=0, le=MAX_DELAY_MS, strict=True)

    @model_validator(mode="after")
    def validate_intervals(self):
        if self.min_delay_ms > self.max_delay_ms:
            raise ValueError("min_delay_ms must not exceed max_delay_ms")
        if self.pre_enter_min_ms > self.pre_enter_max_ms:
            raise ValueError("pre_enter_min_ms must not exceed pre_enter_max_ms")
        return self


class JitterTestRequest(DiagnosticInputRequest):
    @model_validator(mode="after")
    def validate_human_length(self):
        if self.mode == "human" and len(self.text) > MAX_HUMAN_DIAGNOSTIC_CHARS:
            raise ValueError(f"Human typing diagnostics allow at most {MAX_HUMAN_DIAGNOSTIC_CHARS} characters")
        return self


async def _capture_input(req: DiagnosticInputRequest, *, paste=False):
    config = JitterConfig(**req.model_dump(exclude={"text"}))
    simulator = KeystrokeJitterSimulator(config)
    operation = simulator.paste_into_fd if paste else simulator.type_into_fd
    # A bounded local file avoids a pipe filling before its first reader starts.
    with tempfile.TemporaryFile(mode="w+b") as capture:
        try:
            metrics = await asyncio.wait_for(
                operation(capture.fileno(), req.text, append_return=True),
                timeout=DIAGNOSTIC_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            raise FreebuffException(
                "Local input diagnostic exceeded its time limit.", status_code=504,
                error_type="timeout_error", error_code="diagnostic_timeout",
            ) from None
        capture.seek(0)
        captured = capture.read()
    result = metrics.to_dict()
    result["measurement_scope"] = "local_input_simulation"
    result["captured_bytes_len"] = len(captured)
    # Retain the old response field for callers while using a nonblocking sink.
    result["captured_pipe_bytes_len"] = len(captured)
    return result, captured


@router.get("/v1/system/env-audit")
@router.get("/system/env-audit")
async def get_environment_audit():
    """Report the observed environment and process tree, plus local simulations."""
    worker = global_pool.worker
    def inspect():
        pid = worker.get_freebuff_pid() if worker and worker.is_running else None
        return audit_runtime_environment(pid)
    audit = await asyncio.to_thread(inspect)
    audit["worker_running"] = worker.is_running if worker else False
    audit["worker_ready"] = worker.ready_event.is_set() if worker else False
    return audit


@router.post("/v1/system/jitter-test")
@router.post("/system/jitter-test")
async def test_keystroke_jitter(req: JitterTestRequest):
    """Measure local typing with bounded input and a five-second deadline."""
    result, _ = await _capture_input(req)
    result["verified_cps_under_12"] = result["max_cps"] <= 12
    result["cps_limit_applied"] = req.mode == "human"
    return result


@router.post("/v1/system/paste-test")
@router.post("/system/paste-test")
async def test_bracketed_paste(req: DiagnosticInputRequest):
    """Measure a bounded local bracketed-paste simulation."""
    result, captured = await _capture_input(req, paste=True)
    result["contains_bracketed_paste_seq"] = captured.startswith(b"\x1b[200~") and captured.endswith(b"\x1b[201~\r")
    return result


@router.get("/v1/system/jitter-metrics")
@router.get("/system/jitter-metrics")
async def get_latest_jitter_metrics():
    if not global_pool.worker or not global_pool.worker.last_jitter_metrics:
        return {"status": "no_turns_executed_yet"}
    return global_pool.worker.last_jitter_metrics.to_dict()


@router.get("/v1/workspace")
@router.get("/workspace")
async def get_active_workspace():
    cur_workdir = global_pool.worker.workdir if global_pool.worker else WORKER_WORKDIR
    git_info = await asyncio.to_thread(get_workspace_git_info, cur_workdir)
    return {
        "active_workspace": cur_workdir,
        "basename": os.path.basename(cur_workdir),
        "git": git_info,
    }


@router.post("/v1/workspace")
@router.post("/workspace")
async def set_active_workspace(req: SetWorkspaceRequest):
    target = await global_pool.set_workspace(req.path)
    git_info = await asyncio.to_thread(get_workspace_git_info, target)
    return {
        "status": "switched",
        "active_workspace": target,
        "basename": os.path.basename(target),
        "git": git_info,
    }
