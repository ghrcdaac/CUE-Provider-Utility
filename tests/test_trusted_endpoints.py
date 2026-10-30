import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from cue_provider_utility import credential_store, endpoints
from cue_provider_utility.api_client import ApiClient
from cue_provider_utility.models import AppConfig, GlobalArgs

from helpers import PRESIGNED, run_cli, write_config

KEY = "cue_sk_officialofficialofficial12"
OFFICIAL = "https://upload.earthdata.nasa.gov/api"
EVIL = "https://evil.example.com/api"


@pytest.fixture(autouse=True)
def reset_warnings():  # type: ignore[no-untyped-def]
    endpoints._warned.clear()
    yield


def _tampered(tmp: Path) -> Path:
    return write_config(tmp, default_env="prod", environments={"prod": EVIL})


def _mock_upload(httpx_mock, base: str) -> None:  # type: ignore[no-untyped-def]
    fid = str(uuid4())
    httpx_mock.add_response(
        url=f"{base}/v2/upload/prepare-single",
        method="POST",
        json={"file_id": fid, "presigned_url": PRESIGNED},
    )
    httpx_mock.add_response(url=PRESIGNED, method="PUT", headers={"ETag": '"e"'})
    httpx_mock.add_response(
        url=f"{base}/v2/upload/complete-single", method="POST", json={"file_id": fid}
    )


def test_trusted_endpoints_ignores_tampered_config(tmp_path: Path, httpx_mock) -> None:
    cfg = _tampered(tmp_path)
    credential_store.set_key("upload.earthdata.nasa.gov", KEY)
    f = tmp_path / "a.nc"
    f.write_bytes(b"1")
    _mock_upload(httpx_mock, OFFICIAL)
    r = run_cli(
        ["--config", str(cfg), "upload", "-P", str(f), "-c", "C", "--trusted-endpoints"]
    )
    assert r.exit_code == 0, r.output
    hosts = {req.url.host for req in httpx_mock.get_requests()}
    assert "evil.example.com" not in hosts and "upload.earthdata.nasa.gov" in hosts


def test_without_flag_warns_and_never_sends_vault_key(
    tmp_path: Path, httpx_mock
) -> None:
    cfg = _tampered(tmp_path)
    credential_store.set_key("upload.earthdata.nasa.gov", KEY)
    f = tmp_path / "a.nc"
    f.write_bytes(b"1")
    r = run_cli(["--config", str(cfg), "upload", "-P", str(f), "-c", "C"])
    assert "overrides the prod URL to evil.example.com" in r.output
    assert r.exit_code == 1 and "API Key not found" in r.output
    assert not httpx_mock.get_requests()


def test_env_var_enables_trusted_mode(
    tmp_path: Path, httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CUE_UPLOAD_TRUSTED_ENDPOINTS", "1")
    cfg = _tampered(tmp_path)
    credential_store.set_key("upload.earthdata.nasa.gov", KEY)
    f = tmp_path / "a.nc"
    f.write_bytes(b"1")
    _mock_upload(httpx_mock, OFFICIAL)
    r = run_cli(["--config", str(cfg), "upload", "-P", str(f), "-c", "C"])
    assert r.exit_code == 0, r.output


def test_local_needs_allow_local(tmp_path: Path, httpx_mock) -> None:
    cfg = write_config(tmp_path)  # default_env = local
    f = tmp_path / "a.nc"
    f.write_bytes(b"1")
    base = ["--config", str(cfg), "upload", "-P", str(f), "-c", "C", "-t", KEY]
    r = run_cli(base + ["--trusted-endpoints"])
    assert r.exit_code == 2 and "--allow-local" in r.output
    _mock_upload(httpx_mock, "http://localhost:8000")
    r = run_cli(base + ["--trusted-endpoints", "--allow-local"])
    assert r.exit_code == 0, r.output
    r = run_cli(
        ["--config", str(cfg), "--trusted-endpoints", "auth", "status", "--json"]
    )
    assert '"local"' not in r.stdout


def test_authorization_dropped_on_cross_origin_redirect(httpx_mock) -> None:
    httpx_mock.add_response(
        url=f"{OFFICIAL}/v2/me/upload-targets",
        status_code=302,
        headers={"Location": "https://other.example.org/steal"},
    )
    httpx_mock.add_response(url="https://other.example.org/steal", json={})
    config = AppConfig()
    client = ApiClient(config, GlobalArgs(env_cli="prod", config=config), KEY)

    async def go() -> None:
        try:
            await client._request(
                "GET", "/v2/me/upload-targets", expected_status_codes=[200]
            )
        finally:
            await client.close()

    asyncio.run(go())
    first, second = httpx_mock.get_requests()
    assert first.headers["authorization"] == f"Bearer {KEY}"
    assert "authorization" not in second.headers


def test_tls_verify_prefers_ca_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUE_UPLOAD_CA_BUNDLE", "/etc/corp-ca.pem")
    assert endpoints.tls_verify() == "/etc/corp-ca.pem"
    monkeypatch.delenv("CUE_UPLOAD_CA_BUNDLE")
    assert endpoints.tls_verify() is not None
    assert isinstance(
        httpx.AsyncClient(verify=endpoints.tls_verify()), httpx.AsyncClient
    )
