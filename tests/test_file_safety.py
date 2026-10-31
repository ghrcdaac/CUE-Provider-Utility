"""Shared files stay intact when the terminal CLI and the desktop app write at once (plan 22 R-H1)."""

import asyncio
import json
import multiprocessing as mp
from pathlib import Path

from cue_provider_utility.config_manager import load_config, save_config_value
from cue_provider_utility.failed_uploads_handler import FailedUploadsManager
from cue_provider_utility.file_safety import atomic_write_text, locked
from cue_provider_utility.models import FailedUploadEntry


def _set_many(cfg: str, key: str, values: list) -> None:
    for v in values:
        save_config_value(key, v, Path(cfg))


def test_two_processes_changing_different_settings_keep_both(tmp_path):
    cfg = tmp_path / "config.toml"
    save_config_value("retry_attempts", 1, cfg)
    ctx = mp.get_context("spawn")
    a = ctx.Process(
        target=_set_many, args=(str(cfg), "file_concurrency", [2, 3, 4, 5, 6])
    )
    b = ctx.Process(
        target=_set_many, args=(str(cfg), "part_concurrency", [2, 3, 4, 5, 7])
    )
    a.start(), b.start()
    a.join(60), b.join(60)
    assert a.exitcode == 0 and b.exitcode == 0
    final = load_config(cfg)
    # Without the lock + re-read, one process overwrote the other's change.
    assert final.file_concurrency == 6
    assert final.part_concurrency == 7
    assert final.retry_attempts == 1
    assert not (tmp_path / "config.toml.lock").exists()


def test_atomic_write_replaces_whole_file(tmp_path):
    p = tmp_path / "f.json"
    atomic_write_text(p, "old")
    atomic_write_text(p, "new")
    assert p.read_text(encoding="utf-8") == "new"
    assert [x.name for x in tmp_path.iterdir()] == ["f.json"]  # no temp files left


def test_stale_lock_is_taken_over(tmp_path):
    import os
    import time

    target = tmp_path / "config.toml"
    lock = tmp_path / "config.toml.lock"
    lock.write_text("99999")
    old = time.time() - 120
    os.utime(lock, (old, old))
    with locked(target, timeout=1):
        assert lock.exists()
    assert not lock.exists()


def test_unreadable_failed_log_is_kept_not_wiped(tmp_path):
    log = tmp_path / "failed_uploads.json"
    log.write_text("{not json", encoding="utf-8")
    entry = FailedUploadEntry(
        file_path=tmp_path / "a.nc",
        collection="c",
        target_sub_path="",
        env="uat",
        file_size=1,
    )
    asyncio.run(FailedUploadsManager.add_failed_entry(entry, log))
    kept = [
        p
        for p in tmp_path.iterdir()
        if p.name.startswith("failed_uploads.json.unreadable-")
    ]
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "{not json"
    assert len(json.loads(log.read_text(encoding="utf-8"))["failed_uploads"]) == 1
