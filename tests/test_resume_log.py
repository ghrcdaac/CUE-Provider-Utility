"""--resume-file: files finished in an earlier, unfinished run are skipped (plan 22 R-D5)."""

import os
from pathlib import Path

from cue_provider_utility import resume_log
from cue_provider_utility.folder_processor import FileToUpload


def _file(tmp: Path, name: str, data: bytes = b"abc") -> FileToUpload:
    p = tmp / name
    p.write_bytes(data)
    return FileToUpload(p, Path(name), len(data), "2026/10", True)


def test_finished_files_are_skipped_on_the_next_run(tmp_path):
    log_path = tmp_path / "resume.jsonl"
    a, b = _file(tmp_path, "a.nc"), _file(tmp_path, "b.nc")
    log = resume_log.activate(log_path)
    assert log is not None
    log.record(a.local_path, a.size, resume_log.destination("glm", a))

    resume_log.activate(log_path)  # a new run reads the file again
    rest, skipped = resume_log.split_done([a, b], "glm")
    assert [f.local_path.name for f in rest] == ["b.nc"] and skipped == 1
    resume_log.activate(None)


def test_changed_file_or_other_destination_is_uploaded_again(tmp_path):
    log_path = tmp_path / "resume.jsonl"
    a = _file(tmp_path, "a.nc")
    resume_log.activate(log_path).record(
        a.local_path, a.size, resume_log.destination("glm", a)
    )
    resume_log.activate(log_path)
    assert resume_log.split_done([a], "other")[1] == 0  # other collection
    st = a.local_path.stat()
    os.utime(a.local_path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))
    assert resume_log.split_done([a], "glm")[1] == 0  # modified since
    resume_log.activate(None)


def test_a_line_cut_short_by_a_crash_is_ignored(tmp_path):
    log_path = tmp_path / "resume.jsonl"
    log_path.write_text(
        '{"path": "x", "size": 1, "mtime_ns": 1, "target": "t"}\n{"path": "y", "si',
        encoding="utf-8",
    )
    assert len(resume_log.activate(log_path).done) == 1
    resume_log.activate(None)


def test_off_by_default():
    resume_log.activate(None)
    assert resume_log.split_done([1, 2], "c") == ([1, 2], 0)
