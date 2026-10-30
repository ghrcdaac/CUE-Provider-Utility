import asyncio
import json
from pathlib import Path

from cue_provider_utility.failed_uploads_handler import FailedUploadsManager
from cue_provider_utility.models import FailedUploadEntry

from helpers import mock_single_upload, parse_events, run_cli, write_config

KEY = "cue_sk_retryretryretryretry12345"


def _add(tmp: Path, name: str) -> str:
    f = tmp / name
    f.write_bytes(b"r" * 8)
    entry = asyncio.run(
        FailedUploadsManager.add_failed_entry(
            FailedUploadEntry(
                file_path=f,
                collection="C",
                env="local",
                file_size=8,
                checksum="Y2hlY2s=",
                content_type="application/x-netcdf",
            ),
            tmp / "failed_uploads.json",
        )
    )
    assert entry.entry_id
    return entry.entry_id


def test_retry_only_selected_entries(tmp_path: Path, httpx_mock) -> None:
    cfg = write_config(tmp_path)
    first = _add(tmp_path, "a.nc")
    second = _add(tmp_path, "b.nc")
    mock_single_upload(httpx_mock)
    r = run_cli(
        ["--config", str(cfg), "retry", "--entry-id", first, "-t", KEY, "--json-events"]
    )
    assert r.exit_code == 0, r.output
    evs = parse_events(r.stdout)
    done = [e for e in evs if e["type"] == "file_done"]
    assert len(done) == 1 and done[0]["path"].endswith("a.nc")
    left = json.loads((tmp_path / "failed_uploads.json").read_text())["failed_uploads"]
    assert [e["entry_id"] for e in left] == [second]


def test_retry_unknown_entry_id(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    _add(tmp_path, "a.nc")
    r = run_cli(["--config", str(cfg), "retry", "--entry-id", "nope", "-t", KEY])
    assert r.exit_code == 1 and "nope" in r.output
