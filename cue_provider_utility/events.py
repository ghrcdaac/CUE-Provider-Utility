"""Machine-readable event stream (``--json-events``) for the CUE desktop app.

When events mode is on, stdout carries only newline-delimited JSON (one event
per line, schema v1, see ``docs/events-v1.md``). Without it the sink is a
no-op and nothing changes for terminal users.
"""

import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import IO, Any, Dict, List, Literal, Optional, Protocol, Type

from pydantic import BaseModel, ConfigDict

from .redact import redact

PROTOCOL_VERSION = 1


class EventSink(Protocol):
    def emit(self, event: Dict[str, Any]) -> None: ...


class NullSink:
    """Default sink: drops every event."""

    def emit(self, event: Dict[str, Any]) -> None:
        return None


class JsonLinesSink:
    """Writes one compact JSON object per line and flushes (thread-safe)."""

    def __init__(self, stream: Optional[IO[str]] = None) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def emit(self, event: Dict[str, Any]) -> None:
        line = json.dumps(event, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            stream = self._stream or sys.stdout
            stream.write(line)
            stream.flush()


# --- File/batch bookkeeping for one CLI session ---------------------------------


class _FileState:
    __slots__ = ("file_id", "path", "size", "state", "started", "remote_file_id")

    def __init__(self, file_id: str, path: str, size: int) -> None:
        self.file_id = file_id
        self.path = path
        self.size = size
        self.state = "PENDING"
        self.started = time.monotonic()
        self.remote_file_id: Optional[str] = None


class EventSession:
    """Holds the sink, the session id and per-file state for batch totals."""

    def __init__(self, sink: Optional[EventSink] = None) -> None:
        self.sink: EventSink = sink or NullSink()
        self.session_id = str(uuid.uuid4())
        self.enabled = sink is not None and not isinstance(sink, NullSink)
        self.files: Dict[str, _FileState] = {}
        self._by_path: Dict[str, str] = {}
        self.started = time.monotonic()
        self.auth_failed = False
        self._lock = threading.Lock()

    # -- raw emit --
    def emit(self, type_: str, **fields: Any) -> None:
        if not self.enabled:
            return
        event: Dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session_id": self.session_id,
            "type": type_,
        }
        event.update({k: v for k, v in fields.items() if v is not None})
        self.sink.emit(event)

    # -- file helpers --
    def file_id_for(self, path: Any, size: int = 0) -> str:
        """Returns a stable local id for a path (reused across auto-retry rounds)."""
        key = str(path)
        with self._lock:
            fid = self._by_path.get(key)
            if fid is None:
                fid = str(uuid.uuid4())
                self._by_path[key] = fid
                self.files[fid] = _FileState(fid, key, size)
            elif size and not self.files[fid].size:
                self.files[fid].size = size
            return fid

    def stage(self, file_id: str, stage: str, **fields: Any) -> None:
        st = self.files.get(file_id)
        if st is not None:
            st.state = stage
            if fields.get("remote_file_id"):
                st.remote_file_id = str(fields["remote_file_id"])
            if stage == "RETRYING":
                st.started = time.monotonic()
        path = st.path if st else fields.pop("path", None)
        self.emit("file_stage", file_id=file_id, path=path, stage=stage, **fields)

    def file_done(
        self,
        file_id: str,
        sha256_b64: Optional[str],
        content_type: Optional[str],
        remote_path: str,
    ) -> None:
        st = self.files.get(file_id)
        if st is None:
            return
        st.state = "COMPLETED"
        self.emit(
            "file_stage",
            file_id=file_id,
            path=st.path,
            stage="COMPLETED",
            remote_file_id=st.remote_file_id,
        )
        self.emit(
            "file_done",
            file_id=file_id,
            path=st.path,
            remote_file_id=st.remote_file_id,
            sha256_b64=sha256_b64,
            size=st.size,
            content_type=content_type,
            remote_path=remote_path,
            duration_s=round(time.monotonic() - st.started, 3),
        )

    def skip(self, file_id: str) -> None:
        """Marks a scanned-but-excluded item so it never counts toward totals."""
        st = self.files.get(file_id)
        if st is not None:
            st.state = "SKIPPED"

    def mark_cancelled(self) -> None:
        """Emits ``CANCELLED`` for every file that didn't finish."""
        for st in list(self.files.values()):
            if st.state not in ("COMPLETED", "FAILED", "SKIPPED", "CANCELLED"):
                self.stage(st.file_id, "CANCELLED")

    def counts(self) -> Dict[str, int]:
        c = {"succeeded": 0, "failed": 0, "cancelled": 0, "bytes": 0}
        for st in self.files.values():
            if st.state == "COMPLETED":
                c["succeeded"] += 1
                c["bytes"] += st.size
            elif st.state == "CANCELLED":
                c["cancelled"] += 1
            elif st.state in ("FAILED", "RETRY_PENDING", "RETRYING"):
                c["failed"] += 1
        return c

    def batch_done(self, cancelled: bool = False) -> str:
        c = self.counts()
        if cancelled:
            result = "CANCELLED"
        elif c["failed"]:
            result = "BATCH_PARTIAL"
        else:
            result = "BATCH_OK"
        self.emit(
            "batch_done",
            succeeded=c["succeeded"],
            failed=c["failed"],
            cancelled=c["cancelled"],
            bytes=c["bytes"],
            duration_s=round(time.monotonic() - self.started, 3),
            result_code=result,
        )
        return result


_current = EventSession()


def current() -> EventSession:
    return _current


def enabled() -> bool:
    return _current.enabled


def start_session(sink: Optional[EventSink]) -> EventSession:
    """Installs a new session (``sink=None`` → events off)."""
    global _current
    _current = EventSession(sink)
    return _current


def emit(type_: str, **fields: Any) -> None:
    _current.emit(type_, **fields)


def hello(env: str, api_host: Optional[str]) -> None:
    from . import __version__

    emit(
        "hello",
        cli_version=__version__,
        protocol=PROTOCOL_VERSION,
        pid=os.getpid(),
        env=env,
        api_host=api_host,
    )


def notice(
    code: str,
    severity: str,
    title: str,
    text: str,
    notify: bool,
    retryable: bool,
    hint: Optional[str] = None,
    file_id: Optional[str] = None,
    failed_entry_id: Optional[str] = None,
) -> None:
    emit(
        "notice",
        code=code,
        severity=severity,
        title=title,
        text=redact(text),
        notify=notify,
        retryable=retryable,
        hint=hint,
        file_id=file_id,
        failed_entry_id=failed_entry_id,
    )


# --- Schema v1 (pydantic models; used by tests and documented in docs/) ---------

Stage = Literal[
    "HASHING",
    "NEGOTIATING",
    "UPLOADING",
    "VERIFYING",
    "COMPLETED",
    "FAILED",
    "RETRY_PENDING",
    "RETRYING",
    "CANCELLED",
]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")
    v: Literal[1]
    ts: str
    session_id: str
    type: str


class HelloEvent(_Base):
    type: Literal["hello"]
    cli_version: str
    protocol: Literal[1]
    pid: int
    env: str
    api_host: Optional[str] = None


class ScanItemEvent(_Base):
    type: Literal["scan_item"]
    item_id: str
    path: str
    rel_path: str
    size: int
    status: Literal["include", "ignored", "rejected", "empty"]
    reason_code: Optional[str] = None


class ScanSummaryEvent(_Base):
    type: Literal["scan_summary"]
    files: int
    bytes: int
    ignored: int
    rejected: int
    empty: int


class ConfirmRequiredEvent(_Base):
    type: Literal["confirm_required"]
    files: int
    bytes: int


class BatchStartEvent(_Base):
    type: Literal["batch_start"]
    files: int
    bytes: int
    collection: str
    target_path: Optional[str] = None
    file_concurrency: int
    part_concurrency: int


class FileStageEvent(_Base):
    type: Literal["file_stage"]
    file_id: str
    path: Optional[str] = None
    stage: Stage
    remote_file_id: Optional[str] = None
    attempt: Optional[int] = None
    max_attempts: Optional[int] = None


class FileProgressEvent(_Base):
    type: Literal["file_progress"]
    file_id: str
    bytes_done: int
    bytes_total: int
    part: Optional[int] = None
    parts: Optional[int] = None


class FileDoneEvent(_Base):
    type: Literal["file_done"]
    file_id: str
    path: str
    remote_file_id: Optional[str] = None
    sha256_b64: Optional[str] = None
    size: int
    content_type: Optional[str] = None
    remote_path: str
    duration_s: float


class NoticeEvent(_Base):
    type: Literal["notice"]
    code: str
    severity: Literal["success", "info", "warning", "error"]
    title: str
    text: str
    notify: bool
    retryable: bool
    hint: Optional[str] = None
    file_id: Optional[str] = None
    failed_entry_id: Optional[str] = None


class RetryRoundEvent(_Base):
    type: Literal["retry_round"]
    round: int
    max_rounds: int
    files: int


class BatchDoneEvent(_Base):
    type: Literal["batch_done"]
    succeeded: int
    failed: int
    cancelled: int
    bytes: int
    duration_s: float
    result_code: Literal["BATCH_OK", "BATCH_PARTIAL", "CANCELLED"]


EVENT_MODELS: Dict[str, Type[_Base]] = {
    "hello": HelloEvent,
    "scan_item": ScanItemEvent,
    "scan_summary": ScanSummaryEvent,
    "confirm_required": ConfirmRequiredEvent,
    "batch_start": BatchStartEvent,
    "file_stage": FileStageEvent,
    "file_progress": FileProgressEvent,
    "file_done": FileDoneEvent,
    "notice": NoticeEvent,
    "retry_round": RetryRoundEvent,
    "batch_done": BatchDoneEvent,
}


def validate_event(event: Dict[str, Any]) -> _Base:
    """Validates one decoded event against schema v1 (raises on mismatch)."""
    return EVENT_MODELS[event["type"]].model_validate(event)


__all__: List[str] = [
    "EventSink",
    "NullSink",
    "JsonLinesSink",
    "EventSession",
    "current",
    "enabled",
    "start_session",
    "emit",
    "hello",
    "notice",
    "validate_event",
    "EVENT_MODELS",
]
