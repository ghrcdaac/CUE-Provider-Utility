"""Files already uploaded in an earlier, unfinished run (desktop app, ``--resume-file``).

The desktop app passes the same resume file when the user clicks "Start again" after
the app was closed or crashed mid-upload. Each completed file is appended as one JSON
line; on the next run, files with the same path, size, modification time and
destination are skipped instead of being uploaded again. Nothing secret is stored.
"""

import json
import logging
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

Key = Tuple[str, int, int, str]

_active: Optional["ResumeLog"] = None


def _key(path: Path, size: int, target: str) -> Optional[Key]:
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return None
    return (str(path.resolve()), size, mtime_ns, target.strip("/"))


class ResumeLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.done: Set[Key] = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(line)
                    self.done.add((d["path"], d["size"], d["mtime_ns"], d["target"]))
                except (ValueError, KeyError, TypeError):
                    continue  # a line cut short by a crash: that file is uploaded again

    def is_done(self, path: Path, size: int, target: str) -> bool:
        k = _key(path, size, target)
        return k is not None and k in self.done

    def record(self, path: Path, size: int, target: str) -> None:
        k = _key(path, size, target)
        if k is None or k in self.done:
            return
        self.done.add(k)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {"path": k[0], "size": k[1], "mtime_ns": k[2], "target": k[3]}
                    )
                    + "\n"
                )
        except OSError as e:
            logger.warning(f"Couldn't update the resume file: {e}")


def activate(path: Optional[Path]) -> Optional[ResumeLog]:
    """Turns resume tracking on for this run (or off with None)."""
    global _active
    _active = ResumeLog(path) if path else None
    return _active


def active() -> Optional[ResumeLog]:
    return _active


def destination(collection: str, file_task: Any) -> str:
    """Where a scanned file goes (collection, sub-path, folder-relative path)."""
    return f"{collection}/{file_task.target_sub_path or ''}|{file_task.relative_path.as_posix()}"


def split_done(files: List, collection: str) -> Tuple[List, int]:
    """Drops files the resume file says are already uploaded; returns (rest, skipped)."""
    log = _active
    if log is None:
        return files, 0
    rest = [
        f
        for f in files
        if not log.is_done(f.local_path, f.size, destination(collection, f))
    ]
    return rest, len(files) - len(rest)
