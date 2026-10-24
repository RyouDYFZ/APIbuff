import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import API_KEY
from .pool import global_pool
from .routes import chat, models, health, system

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("freebuff.api")

def ensure_cloud_credentials():
    """
    Seeds ~/.config/manicode/credentials.json if CODEBUFF_API_KEY is supplied via environment.
    Enables zero-touch headless cloud deployment on Docker, Render, Koyeb, Hugging Face, or Linux VPS.
    """
    import os
    import json
    token = os.environ.get("CODEBUFF_API_KEY") or os.environ.get("FREEBUFF_AUTH_TOKEN")
    if token:
        config_dir = os.path.expanduser("~/.config/manicode")
        os.makedirs(config_dir, exist_ok=True)
        creds_file = os.path.join(config_dir, "credentials.json")
        creds_data = {}
        if os.path.exists(creds_file):
            try:
                with open(creds_file, "r") as f:
                    creds_data = json.load(f)
            except Exception:
                creds_data = {}

        default_block = creds_data.get("default", {})
        if not default_block.get("authToken") or default_block.get("authToken") != token.strip():
            default_block["authToken"] = token.strip()
            default_block["tokenStore"] = "file"
            default_block["id"] = default_block.get("id") or "cloud-user"
            default_block["email"] = os.getenv("FREEBUFF_USER_EMAIL", default_block.get("email", "cloud@freebuff.local"))
            creds_data["default"] = default_block
            with open(creds_file, "w") as f:
                json.dump(creds_data, f, indent=2)
            logger.info("Automatically initialized cloud credentials from CODEBUFF_API_KEY environment variable.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Ensure credentials and warm Freebuff CLI instance
    ensure_cloud_credentials()
    logger.info("Initializing Freebuff worker pool...")
    await global_pool.initialize()
    yield
    # Shutdown: Cleanly terminate child processes
    logger.info("Shutting down Freebuff worker pool...")
    await global_pool.shutdown()

app = FastAPI(
    title="Freebuff OpenAI-Compatible API Gateway",
    description="High-performance, secure, compliant OpenAI-compatible API proxy for Freebuff CLI",
    version="1.0.0",
    lifespan=lifespan
)

# CORS configuration for client apps (Open WebUI, NextChat, Cursor, Cherry Studio)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Optional API Key Authentication Middleware
@app.middleware("http")
async def api_key_auth_middleware(request: Request, call_next):
    # Skip auth for health and docs
    if request.url.path in ("/", "/health", "/docs", "/openapi.json"):
        return await call_next(request)

    if API_KEY:
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Missing Authorization header", "type": "auth_error"}}
            )
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != API_KEY:
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Invalid API Key", "type": "auth_error"}}
            )

    return await call_next(request)

# Mount API routers
app.include_router(health.router)
app.include_router(models.router)
app.include_router(chat.router)
app.include_router(system.router)
