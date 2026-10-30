import json
from pathlib import Path

import pytest

from cue_provider_utility import credential_store, netrc_handler
from cue_provider_utility.auth_handler import get_api_key
from cue_provider_utility.models import AppConfig, GlobalArgs

from helpers import API, mock_single_upload, run_cli, write_config

KEY = "cue_sk_keyringkeyringkeyring1234"
UAT_HOST = "upload.uat.earthdata.nasa.gov"
PROD_HOST = "upload.earthdata.nasa.gov"


@pytest.fixture(autouse=True)
def clean_files():  # type: ignore[no-untyped-def]
    for p in (credential_store.metadata_path(), netrc_handler.NETRC_PATH):
        if p.exists():
            p.unlink()
    yield


def test_set_key_stdin_is_used_by_upload(tmp_path: Path, httpx_mock) -> None:
    cfg = write_config(tmp_path)
    r = run_cli(["--config", str(cfg), "auth", "set-key", "--stdin"], input=KEY + "\n")
    assert r.exit_code == 0, r.output
    assert credential_store.get_key("localhost") == KEY
    meta = credential_store.load_metadata()["localhost"]
    assert meta["suffix"] == KEY[-4:] and meta["source"] == "cli"
    assert KEY not in credential_store.metadata_path().read_text()

    f = tmp_path / "a.nc"
    f.write_bytes(b"1" * 4)
    mock_single_upload(httpx_mock)
    r = run_cli(["--config", str(cfg), "upload", "-P", str(f), "-c", "C"])
    assert r.exit_code == 0, r.output
    assert "OS keyring" in r.output
    auth = httpx_mock.get_requests()[0].headers["authorization"]
    assert auth == f"Bearer {KEY}"


def test_netrc_users_unaffected_without_keyring(
    tmp_path: Path, httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(credential_store, "available", lambda: False)
    cfg = write_config(tmp_path)
    netrc_handler.save_key_to_netrc(
        "localhost", "cue-cli-user", "cue_sk_netrcnetrcnetrc12345"
    )
    f = tmp_path / "a.nc"
    f.write_bytes(b"1")
    mock_single_upload(httpx_mock)
    r = run_cli(["--config", str(cfg), "upload", "-P", str(f), "-c", "C"])
    assert r.exit_code == 0, r.output
    assert ".netrc file" in r.output
    r = run_cli(["--config", str(cfg), "auth", "set-key", "--stdin"], input=KEY + "\n")
    assert r.exit_code == 1 and "not available" in r.output


def test_status_json_warns_about_older_netrc_key(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, default_env="uat")
    credential_store.set_key(UAT_HOST, KEY)
    netrc_handler.save_key_to_netrc(UAT_HOST, "u", "cue_sk_oldoldoldoldoldoldold1")
    r = run_cli(["--config", str(cfg), "auth", "status", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    uat = data["environments"]["uat"]
    assert uat["source"] == "OS keyring" and uat["suffix"] == KEY[-4:]
    assert any(".netrc" in w for w in uat["warnings"])
    assert data["environments"]["prod"]["present"] is False
    assert KEY not in r.stdout


def test_keys_are_separate_per_environment() -> None:
    config = AppConfig()
    credential_store.set_key(UAT_HOST, "cue_sk_uatuatuatuatuatuatuat1")
    key, _ = get_api_key(None, config, GlobalArgs(env_cli="prod", config=config))
    assert key is None
    credential_store.set_key(PROD_HOST, "cue_sk_prodprodprodprodprod12")
    key, _ = get_api_key(None, config, GlobalArgs(env_cli="uat", config=config))
    assert key == "cue_sk_uatuatuatuatuatuatuat1"
    key, _ = get_api_key(None, config, GlobalArgs(env_cli="prod", config=config))
    assert key == "cue_sk_prodprodprodprodprod12"


def test_shared_config_key_warns_for_other_env(tmp_path: Path) -> None:
    cfg = write_config(tmp_path, api_key="cue_sk_sharedsharedshared1234")
    f = tmp_path / "a.nc"
    f.write_bytes(b"1")
    r = run_cli(
        ["--config", str(cfg), "--env", "uat", "auth", "status", "--env", "uat"]
    )
    assert "shared config.toml key" in r.output


def test_auth_test_updates_metadata(tmp_path: Path, httpx_mock) -> None:
    cfg = write_config(tmp_path)
    credential_store.set_key("localhost", KEY)
    httpx_mock.add_response(
        url=f"{API}/v2/me/upload-targets",
        json={
            "caller": {
                "auth_kind": "api_key",
                "name": "Jane Doe",
                "key": {
                    "name": "Desk",
                    "suffix": "x7Qa",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            },
            "targets": [{"collection_short_name": "secret_list"}],
        },
    )
    r = run_cli(["--config", str(cfg), "auth", "test", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["result"] == "ok" and "secret_list" not in r.stdout
    meta = credential_store.load_metadata()["localhost"]
    assert meta["owner_name"] == "Jane Doe" and meta["expires_at"].startswith("2099")
    status = json.loads(
        run_cli(
            ["--config", str(cfg), "auth", "status", "--json", "--env", "local"]
        ).stdout
    )
    assert status["environments"]["local"]["days_left"] > 1000


@pytest.mark.parametrize("status,code", [(401, 3), (404, 4)])
def test_auth_test_errors(tmp_path: Path, httpx_mock, status: int, code: int) -> None:
    cfg = write_config(tmp_path)
    credential_store.set_key("localhost", KEY)
    httpx_mock.add_response(
        url=f"{API}/v2/me/upload-targets", status_code=status, json={"detail": "x"}
    )
    r = run_cli(["--config", str(cfg), "auth", "test", "--json"])
    assert r.exit_code == code


def test_delete_key_all_sources(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    credential_store.set_key("localhost", KEY)
    netrc_handler.save_key_to_netrc("localhost", "u", "cue_sk_oldoldoldoldoldoldold1")
    r = run_cli(["--config", str(cfg), "auth", "delete-key", "--all-sources", "--yes"])
    assert r.exit_code == 0, r.output
    assert credential_store.get_key("localhost") is None
    assert netrc_handler.get_key_from_netrc("localhost") is None


def test_configure_key_stores_in_keyring_hidden(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    r = run_cli(["--config", str(cfg), "configure", "--key"], input=f"1\n{KEY}\n")
    assert r.exit_code == 0, r.output
    assert credential_store.get_key("localhost") == KEY
    assert KEY not in r.output  # hidden input
    r = run_cli(["--config", str(cfg), "configure", "--key"], input="1\nDELETE\n")
    assert credential_store.get_key("localhost") is None
