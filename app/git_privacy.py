"""Install the Git origin-query guard in the worker's isolated home."""

import os
from pathlib import Path
import tempfile


def prepare_git_privacy_bin(home_dir: str) -> str:
    directory = Path(home_dir).resolve() / "fake_bin"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent.parent / "fake_bin" / "git"
    # Concurrent workers can share an account home. Publish a complete script
    # atomically so no worker can execute a partially copied guard.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".git-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(source.read_bytes())
            os.fchmod(handle.fileno(), 0o700)
        os.replace(temporary, directory / "git")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return str(directory)
