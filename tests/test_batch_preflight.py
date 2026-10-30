import json
from pathlib import Path

from helpers import API, mock_single_upload, parse_events, run_cli, write_config

KEY = "cue_sk_abcdefghijklmnopqrstuvwxyz012345"


def _tree(tmp: Path) -> Path:
    d = tmp / "Granules"
    (d / "day1").mkdir(parents=True)
    (d / "a.nc").write_bytes(b"a" * 5)
    (d / "day1" / "b.nc").write_bytes(b"b" * 7)
    (d / ".DS_Store").write_bytes(b"x")  # system ignore pattern
    return d


def _prepared_paths(httpx_mock) -> list:  # type: ignore[no-untyped-def]
    out = []
    for req in httpx_mock.get_requests():
        if req.url.path.endswith("/prepare-single"):
            body = json.loads(req.content)
            out.append((body["collection_path"] or "", body["file_name"]))
    return sorted(out)


def test_multiple_paths_one_session(tmp_path: Path, httpx_mock) -> None:
    cfg = write_config(tmp_path)
    d = _tree(tmp_path)
    single = tmp_path / "solo.nc"
    single.write_bytes(b"s" * 3)
    for _ in range(3):
        mock_single_upload(httpx_mock)

    result = run_cli(
        ["--config", str(cfg), "upload", "-P", str(d), "-P", str(single)]
        + ["-c", "C", "-t", KEY, "-y", "--json-events"]
    )
    assert result.exit_code == 0, result.output
    evs = parse_events(result.stdout)
    assert len({e["session_id"] for e in evs}) == 1
    summary = next(e for e in evs if e["type"] == "scan_summary")
    assert summary["files"] == 3 and summary["ignored"] == 1
    assert not any(e["type"] == "confirm_required" for e in evs)  # -y given
    assert evs[-1]["succeeded"] == 3
    # Standalone -P semantics: folder name not included.
    paths = {(t.replace(chr(92), "/"), n) for t, n in _prepared_paths(httpx_mock)}
    assert paths == {("", "a.nc"), ("day1", "b.nc"), ("", "solo.nc")}


def test_manifest_preserves_folder_names(tmp_path: Path, httpx_mock) -> None:
    cfg = write_config(tmp_path)
    d = _tree(tmp_path)
    single = tmp_path / "solo.nc"
    single.write_bytes(b"s" * 3)
    manifest = tmp_path / "m.json"
    manifest.write_text(
        json.dumps(
            {
                "items": [
                    {"path": str(d), "target_path": "incoming/Granules"},
                    {"path": str(single), "target_path": "incoming"},
                ]
            }
        )
    )
    for _ in range(3):
        mock_single_upload(httpx_mock)
    result = run_cli(
        ["--config", str(cfg), "upload", "--manifest", str(manifest), "-c", "C"]
        + ["-t", KEY, "-y", "--json-events"]
    )
    assert result.exit_code == 0, result.output
    assert _prepared_paths(httpx_mock) == [
        ("incoming", "solo.nc"),
        ("incoming/Granules", "a.nc"),
        ("incoming/Granules/day1", "b.nc"),
    ]
    evs = parse_events(result.stdout)
    remote = sorted(e["remote_path"] for e in evs if e["type"] == "file_done")
    assert remote == [
        "C/incoming/Granules/a.nc",
        "C/incoming/Granules/day1/b.nc",
        "C/incoming/solo.nc",
    ]


def test_manifest_and_path_are_exclusive(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    m = tmp_path / "m.json"
    m.write_text('{"items":[]}')
    f = tmp_path / "a.nc"
    f.write_bytes(b"1")
    r = run_cli(["--config", str(cfg), "upload", "--manifest", str(m), "-P", str(f)])
    assert r.exit_code == 2
    r = run_cli(["--config", str(cfg), "upload", "-c", "C"])
    assert r.exit_code == 2 and "Missing option" in r.output


def test_confirm_handshake_cancel(tmp_path: Path, httpx_mock) -> None:
    cfg = write_config(tmp_path)
    d = _tree(tmp_path)
    result = run_cli(
        ["--config", str(cfg), "upload", "-P", str(d), "-c", "C", "-t", KEY]
        + ["--json-events"],
        input='{"cmd":"cancel"}\n',
    )
    evs = parse_events(result.stdout)
    types = [e["type"] for e in evs]
    assert "confirm_required" in types and "batch_start" not in types
    assert evs[-1]["result_code"] == "CANCELLED"
    assert not [r for r in httpx_mock.get_requests() if API in str(r.url)]


def test_confirm_handshake_eof_cancels(tmp_path: Path, httpx_mock) -> None:
    cfg = write_config(tmp_path)
    d = _tree(tmp_path)
    result = run_cli(
        ["--config", str(cfg), "upload", "-P", str(d), "-c", "C", "-t", KEY]
        + ["--json-events"],
        input="",
    )
    assert parse_events(result.stdout)[-1]["result_code"] == "CANCELLED"


def test_non_interactive_never_prompts(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    d = _tree(tmp_path)
    result = run_cli(
        ["--config", str(cfg), "upload", "-P", str(d), "-c", "C", "-t", KEY]
        + ["--non-interactive"],
        input="y\n",
    )
    assert result.exit_code == 2
    assert "Proceed with upload?" not in result.output


def test_terminal_folder_prompt_unchanged(tmp_path: Path, httpx_mock) -> None:
    cfg = write_config(tmp_path)
    d = _tree(tmp_path)
    mock_single_upload(httpx_mock)
    mock_single_upload(httpx_mock)
    args = ["--config", str(cfg), "upload", "-P", str(d), "-c", "C", "-t", KEY]
    result = run_cli(args, input="y\n")
    assert result.exit_code == 0, result.output
    assert "Proceed with upload?" in result.output
    assert "Files to be uploaded (first 10 shown):" in result.output


def test_terminal_folder_prompt_declined(tmp_path: Path, httpx_mock) -> None:
    cfg = write_config(tmp_path)
    d = _tree(tmp_path)
    args = ["--config", str(cfg), "upload", "-P", str(d), "-c", "C", "-t", KEY]
    result = run_cli(args, input="n\n")
    assert result.exit_code == 0
    assert "Upload cancelled by user." in result.output
