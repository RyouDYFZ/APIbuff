#!/usr/bin/env python3
"""
Standalone Freebuff Terminal Daemon & Supervisor
Manages a detached Freebuff CLI session running under an interactive zsh -l Shell,
reparented to macOS launchd (PID 1). Exposes a local Unix Domain Socket (UDS) for
lifecycle decoupling between the CLI worker and API callers.
"""

import os
import sys
import json
import asyncio
import logging
import signal
import hashlib
from typing import Optional
from pathlib import Path

# Add parent directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.worker import FreebuffWorker
from app.terminal_env import audit_runtime_environment
from app.jitter import JitterConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("freebuff.daemon")


def default_socket_path() -> str:
    override = os.environ.get("FREEBUFF_DAEMON_SOCKET")
    if override:
        return override
    project_id = hashlib.sha256(os.fsencode(Path(__file__).resolve().parent.parent)).hexdigest()[:16]
    runtime_dir = Path("/tmp") / f"freebuff-daemon-{os.getuid()}-{project_id}"
    runtime_dir.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(runtime_dir, 0o700)
    except OSError:
        pass
    return str(runtime_dir / "daemon.sock")


SOCKET_PATH = default_socket_path()

class DaemonServer:
    def __init__(self, socket_path: Optional[str] = None):
        self.socket_path = socket_path or default_socket_path()
        self.worker = FreebuffWorker()
        self.server = None

    async def start(self):
        logger.info("Initializing standalone Freebuff CLI daemon worker...")
        await self.worker.start()

        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        self.server = await asyncio.start_unix_server(
            self.handle_client,
            path=self.socket_path
        )
        try:
            os.chmod(self.socket_path, 0o600)
        except OSError:
            pass
        logger.info(f"Freebuff Daemon listening on Unix socket: {self.socket_path}")

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await reader.readline()
            if not data:
                writer.close()
                return

            req = json.loads(data.decode())
            action = req.get("action")

            if action == "ping":
                resp = {
                    "status": "ok",
                    "running": self.worker.is_running,
                    "ready": self.worker.ready_event.is_set(),
                    "pid": self.worker.pid
                }
            elif action == "audit":
                resp = audit_runtime_environment(self.worker.pid)
            elif action == "chat":
                prompt = req.get("prompt", "")
                reset = req.get("reset", True)
                result = await self.worker.execute_turn(prompt, reset_context=reset)
                resp = {"status": "ok", "result": result}
            elif action == "model":
                model_key = req.get("model_key")
                await self.worker.ensure_model(model_key)
                resp = {"status": "ok", "model_key": model_key}
            else:
                resp = {"status": "error", "message": f"Unknown action: {action}"}

            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
        except Exception as e:
            logger.error(f"Error handling daemon client: {e}")
            writer.write(json.dumps({"status": "error", "message": str(e)}).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def stop(self):
        logger.info("Stopping daemon...")
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
        await self.worker.stop()
        logger.info("Daemon stopped.")

async def main():
    daemon = DaemonServer()
    loop = asyncio.get_running_loop()

    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await daemon.start()
    try:
        await stop_event.wait()
    finally:
        await daemon.stop()

if __name__ == "__main__":
    asyncio.run(main())
