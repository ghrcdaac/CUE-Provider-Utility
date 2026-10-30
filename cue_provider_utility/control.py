"""Control channel on stdin for ``--json-events`` mode.

The desktop app writes one JSON command per line:
- ``{"cmd":"start"}`` answers ``confirm_required``;
- ``{"cmd":"cancel"}`` cancels the run.
End of input (the parent went away) counts as cancel, so the CLI never keeps
running as an orphan. SIGINT/SIGTERM (and CTRL_BREAK on Windows) map to the
same cancel in events mode only; terminal mode keeps today's Ctrl+C behavior.
"""

import asyncio
import json
import logging
import signal
import sys
import threading
from typing import IO, Any, Awaitable, Callable, Dict, Optional, TypeVar

from .exceptions import UploadCancelledError

logger = logging.getLogger(__name__)
T = TypeVar("T")


def parse_command(line: str) -> Optional[str]:
    line = line.strip()
    if not line:
        return None
    try:
        cmd = json.loads(line).get("cmd")
    except (ValueError, AttributeError):
        logger.warning("Ignoring malformed control line on stdin.")
        return None
    if not isinstance(cmd, str):
        logger.warning("Ignoring control line without a 'cmd'.")
        return None
    return cmd


class ControlChannel:
    """Reads stdin on a daemon thread and exposes start/cancel to asyncio code."""

    def __init__(self, stream: Optional[IO[str]] = None) -> None:
        self._stream = stream if stream is not None else sys.stdin
        self._lock = threading.Lock()
        self.started = False
        self.cancelled = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._start_evt: Optional[asyncio.Event] = None
        self._cancel_evt: Optional[asyncio.Event] = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # -- reader thread --
    def _run(self) -> None:
        try:
            for line in iter(self._stream.readline, ""):
                cmd = parse_command(line)
                if cmd is None:
                    continue
                if cmd == "start":
                    self._signal("start")
                elif cmd == "cancel":
                    self._signal("cancel")
                    return
                else:
                    logger.warning(f"Unknown control command '{cmd}' ignored.")
        except (ValueError, OSError):  # stream closed
            pass
        self._signal("cancel")  # EOF: the parent is gone

    def _signal(self, what: str) -> None:
        with self._lock:
            if what == "start":
                self.started = True
            else:
                self.cancelled = True
            loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._sync_events)
            except RuntimeError:
                pass

    def cancel(self) -> None:
        self._signal("cancel")

    # -- asyncio side --
    def _sync_events(self) -> None:
        if self.started and self._start_evt:
            self._start_evt.set()
        if self.cancelled and self._cancel_evt:
            self._cancel_evt.set()
            if self._start_evt:
                self._start_evt.set()  # wake a pending confirm wait

    def attach(self) -> None:
        """Binds the channel to the running event loop (call inside ``asyncio.run``)."""
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._start_evt = asyncio.Event()
            self._cancel_evt = asyncio.Event()
        self._sync_events()

    async def wait_for_start(self) -> bool:
        if self._start_evt is None:
            self.attach()
        assert self._start_evt is not None
        await self._start_evt.wait()
        return self.started and not self.cancelled

    async def wait_cancel(self) -> None:
        if self._cancel_evt is None:
            self.attach()
        assert self._cancel_evt is not None
        await self._cancel_evt.wait()


_channel: Optional[ControlChannel] = None
_previous_handlers: Dict[int, Any] = {}
# Tests replace stdin with a stream that stays open (see tests/helpers.py).
stream_factory: Optional[Callable[[], IO[str]]] = None


def start(stream: Optional[IO[str]] = None) -> ControlChannel:
    """Starts the stdin reader and maps termination signals to cancel."""
    global _channel
    if stream is None and stream_factory is not None:
        stream = stream_factory()
    _channel = ControlChannel(stream)
    channel = _channel

    def _on_signal(signum: int, frame: Any) -> None:
        channel.cancel()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            _previous_handlers[sig] = signal.signal(sig, _on_signal)
        except (ValueError, OSError):  # not the main thread / unsupported
            pass
    return channel


def stop() -> None:
    """Forgets the channel and restores the previous signal handlers."""
    global _channel
    _channel = None
    for sig, handler in list(_previous_handlers.items()):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, TypeError):
            pass
    _previous_handlers.clear()


def current() -> Optional[ControlChannel]:
    return _channel


async def wait_for_start() -> bool:
    """Waits for ``start`` (True) or ``cancel``/EOF (False)."""
    if _channel is None:
        return False
    return await _channel.wait_for_start()


async def run_cancellable(coro: Awaitable[T]) -> T:
    """Runs ``coro``; a cancel from the control channel cancels it.

    Raises ``UploadCancelledError`` when the run was cancelled.
    """
    channel = _channel
    task = asyncio.ensure_future(coro)
    if channel is None:
        return await task
    channel.attach()
    watcher = asyncio.ensure_future(channel.wait_cancel())
    try:
        await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not watcher.done():
            watcher.cancel()
    if not task.done():
        task.cancel()
    try:
        return await task
    except asyncio.CancelledError:
        from .multipart_uploader import abort_active_multipart_uploads

        await abort_active_multipart_uploads()
        raise UploadCancelledError("Upload cancelled.")
