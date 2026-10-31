"""Safe writes for files that the terminal CLI and the desktop app share.

``config.toml`` and ``failed_uploads.json`` can be changed by two processes at once
(a terminal ``cue-upload`` and the GUI's engine). ``locked`` serialises the
read-modify-write across processes with a lock file next to the target, and
``atomic_write_text`` replaces the file in one step, so a reader never sees a
half-written file.
"""

import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# A lock older than this is left over from a crashed process and is taken over.
STALE_LOCK_SECONDS = 30.0


@contextmanager
def locked(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Holds ``<path>.lock`` while the block runs (cross-process, any OS)."""
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except (FileExistsError, PermissionError):
            try:
                if time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
                    lock.unlink()
                    continue
            except (FileNotFoundError, PermissionError):
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"{path.name} is being changed by another cue-upload process; try again."
                )
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, text: str) -> None:
    """Writes ``text`` to a temp file in the same folder, then swaps it in."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # Windows refuses the swap while another process has the file open for a
        # moment (antivirus, a reader); retry briefly.
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
