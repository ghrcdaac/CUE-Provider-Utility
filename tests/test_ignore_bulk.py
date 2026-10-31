"""`ignore add` with several patterns and --from-file (plan 22 §4)."""

from click.testing import CliRunner

from cue_provider_utility.ignored_files_handler import (
    add_user_ignore_patterns,
    split_patterns,
)
from cue_provider_utility.config_manager import get_user_ignore_patterns
from cue_provider_utility.main import cli_app


def test_split_on_newlines_commas_semicolons_keeps_spaces():
    text = "*.bak, *.tmp;~$*\n# comment\nmy notes.txt\n\n ,; "
    assert split_patterns(text) == ["*.bak", "*.tmp", "~$*", "my notes.txt"]


def test_bulk_add_reports_added_present_invalid(tmp_path):
    cfg = tmp_path / "config.toml"
    added, present, invalid = add_user_ignore_patterns(
        ["*.bak", "*.tmp", "*.bak", "x" * 201], cfg
    )
    assert added == ["*.bak", "*.tmp"]
    assert present == ["*.bak"]
    assert invalid == [("x" * 201, "longer than 200 characters")]
    assert get_user_ignore_patterns(cfg) == ["*.bak", "*.tmp"]


def test_cli_several_patterns_and_file(tmp_path):
    cfg = tmp_path / "config.toml"
    lst = tmp_path / "patterns.txt"
    lst.write_text("*.log\n*.old; *.swp\n", encoding="utf-8")
    r = CliRunner().invoke(
        cli_app,
        [
            "--config",
            str(cfg),
            "ignore",
            "add",
            "*.bak",
            "*.tmp",
            "--from-file",
            str(lst),
        ],
    )
    assert r.exit_code == 0, r.output
    assert get_user_ignore_patterns(cfg) == [
        "*.bak",
        "*.tmp",
        "*.log",
        "*.old",
        "*.swp",
    ]


def test_cli_single_pattern_unchanged(tmp_path):
    cfg = tmp_path / "config.toml"
    r = CliRunner().invoke(cli_app, ["--config", str(cfg), "ignore", "add", "*.bak"])
    assert r.exit_code == 0
    assert get_user_ignore_patterns(cfg) == ["*.bak"]


def test_cli_needs_a_pattern(tmp_path):
    r = CliRunner().invoke(
        cli_app, ["--config", str(tmp_path / "c.toml"), "ignore", "add"]
    )
    assert r.exit_code == 2
