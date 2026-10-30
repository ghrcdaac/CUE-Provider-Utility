import asyncio
import json
from pathlib import Path

import pytest
import toml

from cue_provider_utility import credential_store
from cue_provider_utility.failed_uploads_handler import FailedUploadsManager
from cue_provider_utility.models import FailedUploadEntry

from helpers import API, run_cli, write_config

KEY = "cue_sk_guiguiguiguiguiguigui1234"


@pytest.fixture(autouse=True)
def clean_metadata():  # type: ignore[no-untyped-def]
    p = credential_store.metadata_path()
    if p.exists():
        p.unlink()
    yield


def test_capabilities(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    r = run_cli(["--config", str(cfg), "_gui", "capabilities", "--json"])
    assert r.exit_code == 0
    data = json.loads(r.stdout)
    assert data["protocol"] == 1 and "json-events" in data["features"]
    assert data["keyring_available"] is True


def test_gui_group_hidden_from_help() -> None:
    assert "_gui" not in run_cli(["--help"]).output


def test_config_show_redacts_key(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, api_key=KEY)
    r = run_cli(["--config", str(cfg), "_gui", "config", "show", "--json"])
    data = json.loads(r.stdout)
    assert data["api_key"] == "***" and KEY not in r.stdout
    assert data["file_concurrency"] == 4


def test_config_set_validates(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    base = ["--config", str(cfg), "_gui", "config", "set"]
    assert run_cli(base + ["file_concurrency", "8", "--json"]).exit_code == 0
    assert run_cli(base + ["part_concurrency", "8"]).exit_code == 0
    r = run_cli(base + ["user_ignored_patterns", '["*.tmp","*.bak"]', "--json"])
    assert json.loads(r.stdout)["value"] == ["*.tmp", "*.bak"]
    saved = toml.loads(cfg.read_text())
    assert saved["file_concurrency"] == 8 and saved["part_concurrency"] == 8
    assert run_cli(base + ["file_concurrency", "0"]).exit_code == 2
    assert run_cli(base + ["api_key", KEY]).exit_code == 2
    assert run_cli(base + ["environments.prod", "https://x.example/api"]).exit_code == 2
    r = run_cli(
        base + ["environments.uat", "https://x.example/api", "--allow-advanced"]
    )
    assert r.exit_code == 0, r.output


def test_failed_list_and_remove(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    log = tmp_path / "failed_uploads.json"
    entry = asyncio.run(
        FailedUploadsManager.add_failed_entry(
            FailedUploadEntry(
                file_path=tmp_path / "a.nc", collection="C", env="uat", file_size=1
            ),
            log,
        )
    )
    r = run_cli(["--config", str(cfg), "_gui", "failed", "list", "--json"])
    entries = json.loads(r.stdout)["entries"]
    assert [e["entry_id"] for e in entries] == [entry.entry_id]
    r = run_cli(
        ["--config", str(cfg), "_gui", "failed", "remove", "--entry-id", entry.entry_id]
        + ["--json"]
    )
    assert json.loads(r.stdout)["removed"] == [entry.entry_id]
    assert asyncio.run(FailedUploadsManager.get_failed_entries(log)) == []


def test_targets_prints_json_and_syncs_metadata(tmp_path: Path, httpx_mock) -> None:
    cfg = write_config(tmp_path)
    credential_store.set_key("localhost", KEY)
    httpx_mock.add_response(
        url=f"{API}/v2/me/upload-targets",
        json={
            "caller": {
                "name": "Jane",
                "key": {"suffix": "1234", "expires_at": "2099-01-01"},
            },
            "targets": [{"collection_short_name": "c1", "uploadable": True}],
        },
    )
    r = run_cli(["--config", str(cfg), "_gui", "targets", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data["targets"][0]["collection_short_name"] == "c1"
    assert credential_store.load_metadata()["localhost"]["owner_name"] == "Jane"
    assert KEY not in r.stdout


def test_targets_server_too_old(tmp_path: Path, httpx_mock) -> None:
    cfg = write_config(tmp_path)
    credential_store.set_key("localhost", KEY)
    httpx_mock.add_response(url=f"{API}/v2/me/upload-targets", status_code=404, json={})
    r = run_cli(["--config", str(cfg), "_gui", "targets", "--json"])
    assert r.exit_code == 4 and json.loads(r.stdout)["result"] == "SERVER_TOO_OLD"
