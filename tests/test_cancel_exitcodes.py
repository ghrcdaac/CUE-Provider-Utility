import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from cue_provider_utility import multipart_uploader, uploader

from helpers import API, ControlStream, parse_events, run_cli, write_config

KEY = "cue_sk_abcdefghijklmnopqrstuvwxyz012345"


def test_cancel_mid_multipart_aborts_and_exits_130(
    tmp_path: Path, httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = write_config(tmp_path)
    f = tmp_path / "big.nc"
    f.write_bytes(b"z" * 64)
    # Pretend the file is 2 GiB so the multipart path is used, and make the
    # transfer "slow": it asks for cancel and then waits forever.
    monkeypatch.setattr(uploader, "get_file_size", lambda p: 2 * 1024**3)
    stream = ControlStream()

    async def slow_parts(*args, **kwargs):  # type: ignore[no-untyped-def]
        stream.push('{"cmd":"cancel"}')
        await asyncio.sleep(3600)

    monkeypatch.setattr(multipart_uploader, "_run_all_part_uploads", slow_parts)
    upload_id = "up-1"
    httpx_mock.add_response(
        url=f"{API}/v2/upload/multipart/start",
        method="POST",
        json={"file_id": str(uuid4()), "upload_id": upload_id},
    )
    httpx_mock.add_response(
        url=f"{API}/v2/upload/multipart/abort", method="POST", status_code=204
    )

    result = run_cli(
        ["--config", str(cfg), "upload", "-P", str(f), "-c", "C", "-t", KEY]
        + ["--json-events"],
        control_stream=stream,
    )
    assert result.exit_code == 130, result.output
    aborts = [
        r for r in httpx_mock.get_requests() if r.url.path.endswith("/multipart/abort")
    ]
    assert len(aborts) == 1 and json.loads(aborts[0].content)["upload_id"] == upload_id
    evs = parse_events(result.stdout)
    stages = [e["stage"] for e in evs if e["type"] == "file_stage"]
    assert stages[-1] == "CANCELLED"
    assert evs[-1]["result_code"] == "CANCELLED" and evs[-1]["cancelled"] == 1
    failed_log = tmp_path / "failed_uploads.json"
    assert (
        not failed_log.exists()
        or not json.loads(failed_log.read_text())["failed_uploads"]
    )


def test_cancel_before_start_exits_130(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.nc").write_bytes(b"1")
    result = run_cli(
        ["--config", str(cfg), "upload", "-P", str(d), "-c", "C", "-t", KEY]
        + ["--json-events"],
        control_stream=ControlStream(['{"cmd":"cancel"}']),
    )
    assert result.exit_code == 130
    assert parse_events(result.stdout)[-1]["result_code"] == "CANCELLED"


def test_missing_key_exit_codes(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    f = tmp_path / "a.nc"
    f.write_bytes(b"1")
    args = ["--config", str(cfg), "upload", "-P", str(f), "-c", "C"]
    assert run_cli(args + ["--json-events"]).exit_code == 3
    assert run_cli(args).exit_code == 1  # terminal mode unchanged


def test_usage_error_exit_code(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    r = run_cli(["--config", str(cfg), "upload", "-c", "C", "--json-events"])
    assert r.exit_code == 2


def test_help_is_clean_and_ordered() -> None:
    r = run_cli(["--help"])
    assert r.exit_code == 0
    out = r.output
    for hidden in ["--json-events", "--manifest", "--non-interactive"]:
        assert hidden not in run_cli(["upload", "--help"]).output
    order = [out.index(f"  {c} ") for c in ["upload", "retry", "configure", "ignore"]]
    assert order == sorted(order)
    assert "Examples:" in out
