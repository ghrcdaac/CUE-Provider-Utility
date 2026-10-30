"""Hidden ``cue-upload _gui …`` commands used only by the desktop app.

Not shown in ``--help`` (``CUE_UPLOAD_SHOW_ALL_HELP=1`` shows them) and not
documented for terminal users; see ``docs/gui-integration.md``.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from . import __version__, credential_store
from .auth_commands import ENV_CHOICE, _args_for, fetch_upload_targets
from .auth_commands import sync_caller_metadata
from .auth_handler import SOURCE_CLI, SOURCE_ENV, get_api_key
from .config_manager import get_config, save_config_value
from .events import PROTOCOL_VERSION
from .exceptions import ConfigError
from .failed_uploads_handler import FailedUploadsManager
from .logger_setup import rich_console

FEATURES = [
    "json-events",
    "manifest",
    "preflight-confirm",
    "cancel",
    "keyring",
    "trusted-endpoints",
    "gui-targets",
    "retry-entry",
]

# Keys the app may change. Speed presets: Gentle = 2/2, Standard = 4/4, Fast = 8/8
# (file_concurrency/part_concurrency).
ALLOWED_KEYS = {
    "default_env",
    "retry_attempts",
    "auto_retry",
    "file_concurrency",
    "part_concurrency",
    "multipart_threshold_gb",
    "multipart_chunk_size_mb",
    "log_level",
    "user_ignored_patterns",
    "use_keyring",
}
ADVANCED_KEYS = {
    "allowed_mime_types",
    "denied_mime_types",
    "failed_log_path",
    "log_file_directory",
}
JSON_VALUE_KEYS = {"user_ignored_patterns", "allowed_mime_types", "denied_mime_types"}


def _out(data: Any, as_json: bool, human: str) -> None:
    if as_json:
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        rich_console.print(human)


@click.group("_gui", hidden=True)
def gui_group() -> None:
    """Internal commands for the CUE desktop app."""


@gui_group.command("capabilities")
@click.option("--json", "as_json", is_flag=True)
def capabilities_command(as_json: bool) -> None:
    """Compatibility handshake."""
    data = {
        "cli_version": __version__,
        "protocol": PROTOCOL_VERSION,
        "features": FEATURES,
        "keyring_available": credential_store.available(),
        "keyring_backend": credential_store.backend_name(),
    }
    _out(data, as_json, f"cue-upload {__version__} (protocol {PROTOCOL_VERSION})")


@gui_group.group("config")
def gui_config_group() -> None:
    """Reads or changes settings without prompts."""


@gui_config_group.command("show")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def config_show_command(ctx: click.Context, as_json: bool) -> None:
    """Prints the effective config (the API key is never shown)."""
    cfg = get_config(ctx.obj.config_path_override)
    data = cfg.model_dump(mode="json")
    if data.get("api_key"):
        data["api_key"] = "***"
    _out(data, as_json, json.dumps(data, indent=2))


def _parse_value(key: str, raw: str) -> Any:
    if key in JSON_VALUE_KEYS:
        try:
            value = json.loads(raw)
        except ValueError:
            raise click.BadParameter("Expected a JSON list.", param_hint="VALUE")
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(v, str) for v in value)
        ):
            raise click.BadParameter("Expected a JSON list of strings.")
        return value
    return raw


@gui_config_group.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--allow-advanced", is_flag=True, help="Allow environments.* and paths.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def config_set_command(
    ctx: click.Context, key: str, value: str, allow_advanced: bool, as_json: bool
) -> None:
    """Validated non-interactive write of one setting."""
    if key == "api_key":
        raise click.UsageError("Use 'cue-upload auth set-key' to store keys.")
    advanced = key in ADVANCED_KEYS or key.startswith("environments.")
    if key not in ALLOWED_KEYS and not (advanced and allow_advanced):
        raise click.UsageError(f"Setting '{key}' can't be changed here.")
    parsed = _parse_value(key, value)
    try:
        save_config_value(key, parsed, ctx.obj.config_path_override)
    except (ConfigError, ValueError) as e:
        raise click.BadParameter(str(e).splitlines()[0], param_hint="VALUE")
    new_value: Any = get_config(ctx.obj.config_path_override).model_dump(mode="json")
    for part in key.split("."):
        new_value = new_value.get(part) if isinstance(new_value, dict) else None
    _out({"key": key, "value": new_value}, as_json, f"{key} = {new_value}")


@gui_group.group("failed")
def gui_failed_group() -> None:
    """Reads or dismisses failed uploads."""


def _failed_log(ctx: click.Context, override: Optional[Path]) -> Path:
    if override:
        return override
    return Path(get_config(ctx.obj.config_path_override).failed_log_path)


@gui_failed_group.command("list")
@click.option("-f", "--failed-log", "failed_log", type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def failed_list_command(
    ctx: click.Context, failed_log: Optional[Path], as_json: bool
) -> None:
    """Lists failed uploads."""
    entries = asyncio.run(
        FailedUploadsManager.get_failed_entries(_failed_log(ctx, failed_log))
    )
    data = [e.model_dump(mode="json") for e in entries]
    _out({"entries": data}, as_json, f"{len(data)} failed upload(s).")


@gui_failed_group.command("remove")
@click.option("--entry-id", "entry_ids", multiple=True, required=True)
@click.option("-f", "--failed-log", "failed_log", type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def failed_remove_command(
    ctx: click.Context,
    entry_ids: Tuple[str, ...],
    failed_log: Optional[Path],
    as_json: bool,
) -> None:
    """Dismisses failed-upload entries."""
    path = _failed_log(ctx, failed_log)

    async def run() -> List[str]:
        removed = []
        for entry_id in entry_ids:
            if await FailedUploadsManager.remove_failed_entry(entry_id, path):
                removed.append(entry_id)
        return removed

    removed = asyncio.run(run())
    _out({"removed": removed}, as_json, f"Removed {len(removed)} entry(ies).")


@gui_group.command("targets")
@click.option(
    "--env", "env", type=ENV_CHOICE, help="Environment (default: default_env)."
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def targets_command(ctx: click.Context, env: Optional[str], as_json: bool) -> None:
    """Collections this key may upload to (uses the stored key)."""
    env = (env or str(ctx.obj.config.default_env)).lower()
    args = _args_for(ctx, env)
    host = credential_store.host_for_env(args.config, env)
    key, source = get_api_key(ctx.obj.api_key_cli, args.config, args)
    if not key:
        _out({"result": "AUTH_MISSING", "env": env}, as_json, "No key for this env.")
        sys.exit(3)
    result, data = asyncio.run(fetch_upload_targets(args, key))
    if result != "ok" or data is None:
        _out({"result": result, "env": env}, as_json, f"Targets: {result}")
        sys.exit({"AUTH_INVALID": 3, "SERVER_TOO_OLD": 4}.get(result, 1))
    if host and source not in (SOURCE_CLI, SOURCE_ENV):
        sync_caller_metadata(host, data)
    out: Dict[str, Any] = dict(data)
    out.setdefault("result", "ok")
    _out(out, as_json, f"{len(data.get('targets') or [])} collection(s).")
