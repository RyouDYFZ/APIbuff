import hmac
import logging
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .config import API_KEY, HOST
from .pool import global_pool
from .lifecycle import gateway_control
from .credentials import default_config_dir, write_cli_credentials
from .exceptions import FreebuffException
from .routes import chat, models, health, system, accounts

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("freebuff.api")

# Binds that cannot be reached from other machines without explicit opt-in.
LOOPBACK_BIND_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", ""}


def enforce_startup_guards() -> None:
    """Refuse known-unsafe deployments before serving a single request."""
    if sys.version_info < (3, 11):
        raise RuntimeError(
            "The Freebuff gateway requires Python 3.11 or newer: stream cleanup "
            "uses asyncio.Task.cancelling()."
        )
    bind = (HOST or "").strip()
    if not API_KEY and bind.strip("[]") not in LOOPBACK_BIND_HOSTS:
        raise RuntimeError(
            f"Refusing to serve on {bind or '0.0.0.0'} without FREEBUFF_API_KEY: "
            "the gateway would be unauthenticated on a non-loopback interface. "
            "Set a strong FREEBUFF_API_KEY (clients send it as the bearer token) "
            "or bind FREEBUFF_API_HOST=127.0.0.1 for local-only use."
        )
    if not API_KEY:
        logger.warning(
            "FREEBUFF_API_KEY is empty; serving without authentication because the "
            "bind address is loopback. Set a key before exposing the gateway."
        )

def ensure_cloud_credentials():
    """Seed the official CLI profile for an explicitly supplied cloud token."""
    import os
    token = os.environ.get("CODEBUFF_API_KEY") or os.environ.get("FREEBUFF_AUTH_TOKEN")
    if token:
        write_cli_credentials(
            default_config_dir(), token, email=os.getenv("FREEBUFF_USER_EMAIL")
        )
        logger.info("Initialized cloud credentials from the supplied environment token.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    enforce_startup_guards()
    async with gateway_control():
        try:
            ensure_cloud_credentials()
            logger.info("Initializing Freebuff worker pool...")
            await global_pool.initialize()
            yield
        finally:
            # Also clean partially initialized workers if startup fails.
            logger.info("Shutting down Freebuff worker pool...")
            await global_pool.shutdown()

app = FastAPI(
    title="Freebuff OpenAI-Compatible API Gateway",
    description="High-performance, secure, compliant OpenAI-compatible API proxy for Freebuff CLI",
    version="1.0.0",
    lifespan=lifespan
)

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, error: RequestValidationError):
    errors = error.errors()
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "body") or None
        msg = first.get("msg", "Invalid request")
    else:
        location = None
        msg = "Invalid request"
    return JSONResponse(status_code=422, content={"error": {
        "message": msg,
        "type": "invalid_request_error",
        "param": location,
        "code": "invalid_request",
    }})


@app.exception_handler(FreebuffException)
async def gateway_error_handler(request: Request, error: FreebuffException):
    headers = {}
    if getattr(error, "retry_after", None) is not None:
        headers["Retry-After"] = str(error.retry_after)
    return JSONResponse(status_code=error.status_code, content=error.to_dict(), headers=headers)


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, error: HTTPException):
    detail = error.detail
    if isinstance(detail, dict) and "error" in detail:
        payload = detail
    elif isinstance(detail, dict) and "message" in detail:
        payload = {"error": detail}
    else:
        payload = {"error": {
            "message": str(detail),
            "type": "invalid_request_error" if error.status_code < 500 else "api_error",
            "param": None,
            "code": "http_error",
        }}
    return JSONResponse(status_code=error.status_code, content=payload, headers=error.headers)

# Pure ASGI auth preserves stream cancellation without spawning an intermediary
# response task or converting a cancelled preflight into a server error.
class APIKeyAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope["path"] in ("/", "/health", "/docs", "/openapi.json")
            or not API_KEY
        ):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        auth_header = request.headers.get("Authorization")
        parts = auth_header.split() if auth_header else []
        if len(parts) != 2 or parts[0].lower() != "bearer" or not hmac.compare_digest(parts[1], API_KEY):
            response = JSONResponse(status_code=401, content={"error": {
                "message": "Invalid API Key" if auth_header else "Missing Authorization header",
                "type": "authentication_error",
                "param": None,
                "code": "invalid_api_key",
            }})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


# Requests larger than this are rejected before their body is read. The largest
# legitimate payloads are ultra-long prompts (128k characters plus JSON and
# history replay), well below this bound.
MAX_REQUEST_BODY_BYTES = 20 * 1024 * 1024


class RequestBodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for name, value in scope.get("headers", []):
                if name.lower() == b"content-length":
                    try:
                        length = int(value)
                    except ValueError:
                        length = -1
                    if length > MAX_REQUEST_BODY_BYTES:
                        response = JSONResponse(status_code=413, content={"error": {
                            "message": "Request body exceeds the supported size limit.",
                            "type": "invalid_request_error",
                            "param": None,
                            "code": "request_too_large",
                        }})
                        await response(scope, receive, send)
                        return

            received_bytes = 0

            async def limited_receive():
                nonlocal received_bytes
                message = await receive()
                if message.get("type") == "http.request":
                    chunk = message.get("body", b"")
                    received_bytes += len(chunk)
                    if received_bytes > MAX_REQUEST_BODY_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail={
                                "message": "Request body exceeds the supported size limit.",
                                "type": "invalid_request_error",
                                "param": None,
                                "code": "request_too_large",
                            },
                        )
                return message

            await self.app(scope, limited_receive, send)
            return
        await self.app(scope, receive, send)


app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(APIKeyAuthMiddleware)

# Added last so CORS wraps authentication: browser preflights do not carry the
# bearer token, while every actual API request still passes through auth.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API routers
app.include_router(health.router)
app.include_router(models.router)
app.include_router(chat.router)
app.include_router(system.router)
app.include_router(accounts.router)
