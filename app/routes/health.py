import asyncio
import time
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from ..pool import global_pool
from ..auth_probe import verify_token

router = APIRouter()
SERVICE_START_TIME = time.monotonic()


@router.get("/")
@router.get("/health")
async def health_check():
    """Public local liveness only: no credentials, identity, or upstream probes.

    Reports 503 while no worker is running so containers, compose healthchecks,
    and start scripts cannot mistake a worker-less gateway for a serving one.
    """
    worker_count = sum(bool(worker.is_running) for worker in global_pool.workers.values())
    payload = {
        "status": "ok" if worker_count else "unhealthy",
        "worker_count": worker_count,
        "uptime_seconds": max(0, int(time.monotonic() - SERVICE_START_TIME)),
    }
    if not worker_count:
        return JSONResponse(status_code=503, content=payload)
    return payload


@router.get("/v1/auth")
@router.get("/auth")
async def get_auth_status(force: bool = False):
    """Explicit identity diagnostic behind the same auth as the other APIs."""
    return await asyncio.to_thread(verify_token, force=force)
