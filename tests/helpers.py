"""Shared helpers for CLI end-to-end tests (mocked backend, events parsing)."""

import io
import json
import queue
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import toml
from click.testing import CliRunner, Result

from cue_provider_utility import config_manager, control, events
from cue_provider_utility.logger_setup import rich_console
from cue_provider_utility.main import cli_app

NL = chr(10)
API = "http://localhost:8000"
PRESIGNED = (
    "https://s3.example.com/bucket/obj?X-Amz-Signature=abc123&X-Amz-Credential=cred"
)


def reset_cli_state() -> None:
    """Undo process-wide state a previous CLI invocation may have left behind."""
    rich_console.quiet = False
    events.start_session(None)
    control.stop()
    control.stream_factory = None
    config_manager._cached_config = None
    config_manager._cached_config_path = None


def write_config(tmp: Path, **overrides: Any) -> Path:
    cfg = {
        "default_env": "local",
        "failed_log_path": str(tmp / "failed_uploads.json"),
        "log_file_directory": str(tmp / "logs"),
        "auto_retry": False,
        "retry_attempts": 1,
    }
    cfg.update(overrides)
    path = tmp / "config.toml"
    path.write_text(toml.dumps(cfg), encoding="utf-8")
    return path


def mock_single_upload(httpx_mock: Any, prepare_status: int = 200) -> str:
    file_id = str(uuid4())
    if prepare_status != 200:
        httpx_mock.add_response(
            url=f"{API}/v2/upload/prepare-single",
            method="POST",
            status_code=prepare_status,
            json={
                "detail": "Invalid or expired API key cue_sk_SECRETSECRETSECRETSECRET"
            },
        )
        return file_id
    httpx_mock.add_response(
        url=f"{API}/v2/upload/prepare-single",
        method="POST",
        json={"file_id": file_id, "presigned_url": PRESIGNED},
    )
    httpx_mock.add_response(
        url=PRESIGNED, method="PUT", status_code=200, headers={"ETag": '"etag1"'}
    )
    httpx_mock.add_response(
        url=f"{API}/v2/upload/complete-single",
        method="POST",
        json={"file_id": file_id},
    )
    return file_id


class ControlStream(io.TextIOBase):
    """Stand-in for the desktop app's stdin pipe: stays open until closed."""

    def __init__(self, lines: Optional[List[str]] = None) -> None:
        self._q: "queue.Queue[str]" = queue.Queue()
        for line in lines or []:
            self.push(line)

    def push(self, line: str) -> None:
        self._q.put(line if line.endswith(NL) else line + NL)

    def close_pipe(self) -> None:
        self._q.put("")

    def readline(self, size: int = -1) -> str:  # type: ignore[override]
        return self._q.get()


def run_cli(
    args: List[str],
    input: Optional[str] = None,
    control_stream: Optional[ControlStream] = None,
) -> Result:
    """Runs the CLI in-process. In events mode stdin stays open (like the GUI's
    pipe) unless ``input`` is given; ``input=""`` simulates an immediate EOF."""
    reset_cli_state()
    if "--json-events" in args and input is None:
        stream = control_stream or ControlStream()
        control.stream_factory = lambda: stream  # type: ignore[assignment,return-value]
    runner = CliRunner()
    try:
        return runner.invoke(cli_app, args, input=input, catch_exceptions=False)
    finally:
        reset_cli_state()


def parse_events(stdout: str) -> List[Dict[str, Any]]:
    out = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        events.validate_event(event)
        out.append(event)
    return out
