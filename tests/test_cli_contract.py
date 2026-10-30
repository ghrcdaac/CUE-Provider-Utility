"""CLI contract guardrail.

Captures every command's options and ``--help`` text and compares them to a
stored snapshot. New commands/options may be added (regenerate the snapshot
with ``CUE_UPDATE_SNAPSHOTS=1 pytest tests/test_cli_contract.py``), but an
existing command or option must never be removed or renamed.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict

import click
from click.testing import CliRunner

from cue_provider_utility.main import apply_help_visibility, cli_app

FAKE_HOME = Path.home()  # redirected to a temp folder by conftest.py

SNAPSHOT = Path(__file__).parent / "snapshots" / "cli_contract.json"


def _normalize(text: str) -> str:
    home = str(FAKE_HOME.resolve())
    for variant in {home, str(FAKE_HOME), home.replace("\\", "/")}:
        text = text.replace(variant, "~")
    text = text.replace("\\", "/")
    return re.sub(r"\s+", " ", text).strip()


def _walk(cmd: click.Command, path: tuple) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    params = []
    for p in cmd.params:
        params.append(
            {
                "name": p.name,
                "opts": list(p.opts) + list(p.secondary_opts),
                "kind": type(p).__name__,
                "required": p.required,
                "multiple": p.multiple,
                "hidden": bool(getattr(p, "hidden", False)),
            }
        )
    out[" ".join(path) or "<root>"] = {
        "hidden": bool(cmd.hidden),
        "params": params,
    }
    if isinstance(cmd, click.Group):
        for name, sub in cmd.commands.items():
            out.update(_walk(sub, path + (name,)))
    return out


def _help(args: list, env: Dict[str, str] | None = None) -> str:
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        args + ["--help"],
        env=env,
        terminal_width=10000,
        max_content_width=10000,
    )
    assert result.exit_code == 0, result.output
    return _normalize(result.output)


def _current() -> Dict[str, Any]:
    structure = _walk(cli_app, ())

    def helps() -> Dict[str, str]:
        return {
            key: _help([] if key == "<root>" else key.split(" ")) for key in structure
        }

    normal = helps()
    apply_help_visibility(True)  # CUE_UPLOAD_SHOW_ALL_HELP=1
    try:
        show_all = helps()
    finally:
        apply_help_visibility(False)
    return {"structure": structure, "help": normal, "help_show_all": show_all}


def test_cli_contract_snapshot() -> None:
    current = _current()
    if os.environ.get("CUE_UPDATE_SNAPSHOTS") == "1" or not SNAPSHOT.exists():
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    stored = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    # Guardrail: nothing that existed may disappear or be renamed.
    for cmd, info in stored["structure"].items():
        assert cmd in current["structure"], f"command removed: {cmd}"
        now = {tuple(p["opts"]): p for p in current["structure"][cmd]["params"]}
        for p in info["params"]:
            assert tuple(p["opts"]) in now, f"option removed/renamed: {cmd} {p['opts']}"
            assert now[tuple(p["opts"])]["name"] == p["name"], (cmd, p["opts"])

    # Any other difference (additions, wording) must be reviewed by regenerating.
    assert current == stored, (
        "CLI contract changed. If this is an intended addition, regenerate with "
        "CUE_UPDATE_SNAPSHOTS=1."
    )
