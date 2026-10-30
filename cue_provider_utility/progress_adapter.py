"""Progress objects for the uploaders.

``JsonProgress`` implements the subset of the Rich ``Progress`` API the
uploaders call (``add_task``, ``update``, ``reset``, ``start_task``,
``remove_task``, context manager) and turns it into ``file_progress`` events,
so the upload code runs unchanged in ``--json-events`` mode. Only tasks bound to
a file (``bind``) produce events; helper tasks (checksum bars, the folder bar)
are tracked silently.
"""

import time
from typing import Any, Dict, Optional

from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn
from rich.progress import TimeElapsedColumn

from . import events
from .logger_setup import rich_console

THROTTLE_S = 0.25  # at most 4 progress events per second per file


class _Task:
    __slots__ = ("total", "completed", "file_id", "last_emit", "description")

    def __init__(self, total: Optional[float], description: str) -> None:
        self.total = total
        self.completed = 0.0
        self.file_id: Optional[str] = None
        self.last_emit = 0.0
        self.description = description


class JsonProgress:
    """Rich-Progress-compatible object that emits throttled ``file_progress`` events."""

    def __init__(self) -> None:
        self._tasks: Dict[int, _Task] = {}
        self._next = 0

    def __enter__(self) -> "JsonProgress":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def add_task(
        self,
        description: str,
        start: bool = True,
        total: Optional[float] = 100.0,
        completed: int = 0,
        visible: bool = True,
        **fields: Any,
    ) -> int:
        task_id = self._next
        self._next += 1
        task = _Task(total, description)
        task.completed = float(completed)
        self._tasks[task_id] = task
        return task_id

    def bind(self, task_id: Any, file_id: str) -> None:
        """Links a task to a file so its updates become ``file_progress`` events."""
        task = self._tasks.get(task_id)
        if task is not None:
            task.file_id = file_id

    def file_id(self, task_id: Any) -> Optional[str]:
        task = self._tasks.get(task_id)
        return task.file_id if task else None

    def start_task(self, task_id: Any) -> None:
        return None

    def stop_task(self, task_id: Any) -> None:
        return None

    def remove_task(self, task_id: Any) -> None:
        self._tasks.pop(task_id, None)

    def reset(
        self,
        task_id: Any,
        start: bool = True,
        total: Optional[float] = None,
        completed: int = 0,
        description: Optional[str] = None,
        **fields: Any,
    ) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        if total is not None:
            task.total = total
        task.completed = float(completed)
        if description is not None:
            task.description = description
        task.last_emit = 0.0

    def update(
        self,
        task_id: Any,
        total: Optional[float] = None,
        completed: Optional[float] = None,
        advance: Optional[float] = None,
        description: Optional[str] = None,
        **fields: Any,
    ) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        if total is not None:
            task.total = total
        if completed is not None:
            task.completed = float(completed)
        if advance is not None:
            task.completed += advance
        if description is not None:
            task.description = description
        if task.file_id is None or (completed is None and advance is None):
            return
        total_now = int(task.total or 0)
        done = int(task.completed)
        now = time.monotonic()
        final = total_now > 0 and done >= total_now
        if final or now - task.last_emit >= THROTTLE_S:
            task.last_emit = now
            events.emit(
                "file_progress",
                file_id=task.file_id,
                bytes_done=min(done, total_now) if total_now else done,
                bytes_total=total_now,
            )


def make_progress() -> Any:
    """Returns a ``JsonProgress`` in events mode, otherwise the usual Rich bar."""
    if events.enabled():
        return JsonProgress()
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=rich_console,
    )


def stage(progress: Any, task_id: Any, stage_name: str, **fields: Any) -> None:
    """Emits a ``file_stage`` event for the file bound to ``task_id`` (no-op for Rich)."""
    if isinstance(progress, JsonProgress):
        file_id = progress.file_id(task_id)
        if file_id:
            events.current().stage(file_id, stage_name, **fields)
