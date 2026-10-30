import json
import logging
from pathlib import Path

import pytest

from cue_provider_utility import events, messages
from cue_provider_utility.exceptions import APIRequestError, UploadError
from cue_provider_utility.progress_adapter import JsonProgress
from cue_provider_utility.redact import RedactingFilter, RedactingFormatter, redact

from helpers import (
    ControlStream,
    mock_single_upload,
    parse_events,
    run_cli,
    write_config,
)

KEY = "cue_sk_abcdefghijklmnopqrstuvwxyz012345"


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    return write_config(tmp_path)


def _upload(cfg: Path, path: Path, *extra: str, input=None, control_stream=None):  # type: ignore[no-untyped-def]
    return run_cli(
        ["--config", str(cfg), "upload", "-P", str(path), "-c", "COLL", "-t", KEY]
        + list(extra),
        input=input,
        control_stream=control_stream,
    )


def test_single_file_events(tmp_path: Path, cfg: Path, httpx_mock) -> None:
    f = tmp_path / "a.nc"
    f.write_bytes(b"x" * 1000)
    remote_id = mock_single_upload(httpx_mock)

    result = _upload(cfg, f, "--json-events")
    assert result.exit_code == 0, result.output
    evs = parse_events(result.stdout)
    types = [e["type"] for e in evs]
    assert types[0] == "hello" and types[-1] == "batch_done"
    assert "batch_start" in types and "file_done" in types
    stages = [e["stage"] for e in evs if e["type"] == "file_stage"]
    assert stages == ["HASHING", "NEGOTIATING", "UPLOADING", "VERIFYING", "COMPLETED"]
    done = next(e for e in evs if e["type"] == "file_done")
    assert done["remote_file_id"] == remote_id
    assert done["remote_path"] == "COLL/a.nc"
    assert done["size"] == 1000
    progress = [e for e in evs if e["type"] == "file_progress"]
    assert progress and progress[-1]["bytes_done"] == 1000
    codes = [e["code"] for e in evs if e["type"] == "notice"]
    assert codes == ["UPLOAD_OK", "BATCH_OK"]
    assert evs[-1]["result_code"] == "BATCH_OK" and evs[-1]["succeeded"] == 1
    assert len({e["session_id"] for e in evs}) == 1
    # No secrets anywhere in the stream.
    assert KEY not in result.stdout and "abc123" not in result.stdout


def test_folder_events(tmp_path: Path, cfg: Path, httpx_mock) -> None:
    d = tmp_path / "data"
    (d / "sub").mkdir(parents=True)
    (d / "one.nc").write_bytes(b"1" * 10)
    (d / "sub" / "two.nc").write_bytes(b"2" * 20)
    (d / "empty.nc").write_bytes(b"")
    mock_single_upload(httpx_mock)
    mock_single_upload(httpx_mock)

    # No -y: events mode never prompts on the terminal; it waits for "start".
    stream = ControlStream(['{"cmd":"start"}'])
    result = _upload(cfg, d, "--json-events", control_stream=stream)
    assert result.exit_code == 0, result.output
    evs = parse_events(result.stdout)
    types = [e["type"] for e in evs]
    assert types.index("scan_summary") < types.index("confirm_required")
    assert types.index("confirm_required") < types.index("batch_start")
    scan = {e["rel_path"]: e["status"] for e in evs if e["type"] == "scan_item"}
    assert scan == {"one.nc": "include", "sub/two.nc": "include", "empty.nc": "empty"}
    start = next(e for e in evs if e["type"] == "batch_start")
    assert start["files"] == 2 and start["bytes"] == 30
    paths = sorted(e["remote_path"] for e in evs if e["type"] == "file_done")
    assert paths == ["COLL/one.nc", "COLL/sub/two.nc"]
    assert evs[-1]["succeeded"] == 2 and evs[-1]["failed"] == 0


def test_failure_events(tmp_path: Path, cfg: Path, httpx_mock) -> None:
    f = tmp_path / "a.nc"
    f.write_bytes(b"x" * 10)
    mock_single_upload(httpx_mock, prepare_status=401)

    result = _upload(cfg, f, "--json-events")
    assert result.exit_code == 3  # auth failure exit code in events mode
    evs = parse_events(result.stdout)
    stages = [e["stage"] for e in evs if e["type"] == "file_stage"]
    assert stages[-1] == "FAILED"
    notice = next(e for e in evs if e["type"] == "notice")
    assert notice["code"] == "AUTH_INVALID" and notice["severity"] == "error"
    assert notice["failed_entry_id"]
    assert "SECRETSECRET" not in result.stdout
    failed = json.loads((tmp_path / "failed_uploads.json").read_text())
    assert failed["failed_uploads"][0]["entry_id"] == notice["failed_entry_id"]
    assert evs[-1]["result_code"] == "BATCH_PARTIAL" and evs[-1]["failed"] == 1


def test_auto_retry_reports_retry_pending_then_completed(
    tmp_path: Path, httpx_mock
) -> None:
    cfg = write_config(tmp_path, auto_retry=True, retry_attempts=1)
    f = tmp_path / "a.nc"
    f.write_bytes(b"x" * 10)
    mock_single_upload(httpx_mock, prepare_status=500)
    mock_single_upload(httpx_mock)

    result = _upload(cfg, f, "--json-events")
    assert result.exit_code == 0, result.output
    evs = parse_events(result.stdout)
    stages = [e["stage"] for e in evs if e["type"] == "file_stage"]
    assert "RETRY_PENDING" in stages and "FAILED" not in stages
    assert stages.index("RETRY_PENDING") < stages.index("RETRYING")
    assert stages[-1] == "COMPLETED"
    assert any(e["type"] == "retry_round" for e in evs)
    assert evs[-1]["result_code"] == "BATCH_OK"


def test_terminal_mode_has_no_stdout(tmp_path: Path, cfg: Path, httpx_mock) -> None:
    f = tmp_path / "a.nc"
    f.write_bytes(b"x" * 10)
    mock_single_upload(httpx_mock)
    result = _upload(cfg, f)
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "Upload process finished successfully" in result.stderr


def test_json_progress_throttles() -> None:
    sink_events = []

    class Sink:
        def emit(self, event):  # type: ignore[no-untyped-def]
            sink_events.append(event)

    events.start_session(Sink())  # type: ignore[arg-type]
    try:
        p = JsonProgress()
        t = p.add_task("x", total=1000)
        p.bind(t, "fid")
        for _ in range(100):
            p.update(t, advance=10)
        progress = [e for e in sink_events if e["type"] == "file_progress"]
        assert progress[-1]["bytes_done"] == 1000  # final event always sent
        assert len(progress) <= 3  # throttled (first + final within 0.25 s)
    finally:
        events.start_session(None)


def test_redaction_patterns() -> None:
    text = (
        f"Authorization: Bearer {KEY} key={KEY} "
        "url=https://s3/x?X-Amz-Signature=deadbeef&X-Amz-Credential=AKIA/1&"
        "X-Amz-Security-Token=tok "
        '{"refresh_token": "rt-123", "access_token":"at-456"}'
    )
    out = redact(text)
    for secret in [KEY, "deadbeef", "AKIA/1", "tok ", "rt-123", "at-456"]:
        assert secret not in out
    assert "X-Amz-Signature=[REDACTED]" in out


def test_log_records_are_redacted(tmp_path: Path) -> None:
    log_file = tmp_path / "t.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(RedactingFormatter("%(message)s"))
    handler.addFilter(RedactingFilter())
    log = logging.getLogger("redaction-test")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.info("key %s url %s", KEY, "https://s3/x?X-Amz-Signature=sig999")
        try:
            raise ValueError(f"boom {KEY}")
        except ValueError:
            log.exception("failed")
    finally:
        log.removeHandler(handler)
        handler.close()
    content = log_file.read_text(encoding="utf-8")
    assert KEY not in content and "sig999" not in content
    assert "cue_sk_[REDACTED]" in content


def test_map_exception_codes() -> None:
    assert (
        messages.map_exception(APIRequestError("x", status_code=401)) == "AUTH_INVALID"
    )
    assert (
        messages.map_exception(
            UploadError(
                "wrapped",
                original_exception=APIRequestError(
                    "User is not assigned to provider", status_code=403
                ),
            )
        )
        == "NOT_ASSIGNED"
    )
    assert (
        messages.map_exception(
            APIRequestError("Collection X not found", status_code=404)
        )
        == "COLLECTION_NOT_FOUND"
    )
    assert messages.CATALOG["NETWORK"].retryable
