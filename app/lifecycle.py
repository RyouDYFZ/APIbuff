"""Project-scoped native gateway shutdown, without port scans or PID files."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import os
from pathlib import Path
import signal
import socket
import stat
import time
import uuid


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def runtime_directory():
    project_id = hashlib.sha256(os.fsencode(PROJECT_ROOT)).hexdigest()[:16]
    # macOS TMPDIR paths can exceed the Unix socket path limit.
    return Path("/tmp") / f"freebuff-api-{os.getuid()}-{project_id}"


def _validate_directory(directory):
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError(f"Gateway control directory must be private and owned by this user: {directory}")


def ensure_file_descriptor_limit(target: int = 4096) -> tuple[int, int]:
    """
    Ensure the process file descriptor limit (RLIMIT_NOFILE) is raised to at least target.
    Prevents PTY allocation exhaustion and socket leak crashes under high concurrency.
    Returns (soft, hard) limits.
    """
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < target:
            new_soft = min(target, hard) if hard != getattr(resource, "RLIM_INFINITY", -1) else target
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
            except (ValueError, OSError):
                if hard != getattr(resource, "RLIM_INFINITY", -1) and hard > soft:
                    try:
                        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
                    except (ValueError, OSError):
                        pass
        return resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ImportError, AttributeError, Exception):
        return 0, 0


@asynccontextmanager
async def gateway_control(directory=None, on_stop=None):
    """Only this running process handles and signals itself on a stop request."""
    ensure_file_descriptor_limit(4096)
    directory = Path(directory) if directory is not None else runtime_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _validate_directory(directory)
    path = directory / f"gateway-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"
    loop = asyncio.get_running_loop()
    on_stop = on_stop or (lambda: os.kill(os.getpid(), signal.SIGTERM))

    async def handle(reader, writer):
        try:
            if await asyncio.wait_for(reader.readline(), timeout=2) == b"stop\n":
                writer.write(b"stopping\n")
                await writer.drain()
                loop.call_soon(on_stop)
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(path))
    os.chmod(path, 0o600)
    try:
        yield path
    finally:
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)


def stop_registered_gateways(directory=None, timeout=10):
    directory = Path(directory) if directory is not None else runtime_directory()
    if not directory.exists():
        print("No registered native gateway is running for this project.")
        return 0
    _validate_directory(directory)
    requested = []
    failed = False
    for path in sorted(directory.glob("gateway-*.sock")):
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            continue
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(path))
                client.sendall(b"stop\n")
                with client.makefile("rb") as response:
                    if response.readline(32) != b"stopping\n":
                        raise RuntimeError("unexpected control response")
            requested.append(path)
        except (FileNotFoundError, ConnectionRefusedError):
            path.unlink(missing_ok=True)
        except (OSError, RuntimeError) as exc:
            print(f"Could not stop registered gateway {path.name}: {exc}")
            failed = True
    deadline = time.monotonic() + timeout
    while requested and time.monotonic() < deadline:
        requested = [path for path in requested if path.exists()]
        if requested:
            time.sleep(0.05)
    if requested:
        print("Gateway shutdown timed out; no unrelated process was signaled.")
        failed = True
    elif not failed:
        print("Registered native gateways and their workers have stopped.")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(stop_registered_gateways())
