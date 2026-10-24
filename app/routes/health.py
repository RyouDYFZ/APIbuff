import asyncio
import time
from fastapi import APIRouter
from ..pool import global_pool
from ..auth_probe import verify_token

router = APIRouter()
SERVICE_START_TIME = time.monotonic()


@router.get("/")
@router.get("/health")
async def health_check():
    """Public local liveness only: no credentials, identity, or upstream probes."""
    worker_count = sum(bool(worker.is_running) for worker in global_pool.workers.values())
    return {
        "status": "ok" if worker_count else "degraded",
        "worker_count": worker_count,
        "uptime_seconds": max(0, int(time.monotonic() - SERVICE_START_TIME)),
    }


@router.get("/v1/auth")
@router.get("/auth")
async def get_auth_status(force: bool = False):
    """Explicit identity diagnostic behind the same auth as the other APIs."""
    return await asyncio.to_thread(verify_token, force=force)
