import time
from fastapi import APIRouter
from ..pool import global_pool
from ..config import DEFAULT_MODEL
from ..auth_probe import verify_token

router = APIRouter()
SERVICE_START_TIME = time.time()

@router.get("/")
@router.get("/health")
async def health_check():
    """Returns the service health status, worker state, active configuration, and credential health."""
    is_ready = False
    worker_running = False
    pid = None

    if global_pool.worker is not None:
        worker_running = global_pool.worker.is_running
        is_ready = global_pool.worker.ready_event.is_set()
        pid = global_pool.worker.pid

    auth_info = verify_token()

    return {
        "status": "ok" if (worker_running and auth_info.get("valid")) else "degraded" if worker_running else "error",
        "service": "freebuff-api-service",
        "version": "1.0.0",
        "uptime_seconds": int(time.time() - SERVICE_START_TIME),
        "auth": auth_info,
        "worker": {
            "running": worker_running,
            "ready": is_ready,
            "pid": pid,
            "default_model": DEFAULT_MODEL,
            "active_session": global_pool.active_session_id
        }
    }

@router.get("/v1/auth")
@router.get("/auth")
async def get_auth_status():
    """Returns detailed Freebuff authentication status and troubleshooting actions."""
    return verify_token(force=True)
