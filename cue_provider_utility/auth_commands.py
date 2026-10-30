"""``cue-upload auth``: API keys per environment in the OS keyring."""

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import click

from . import credential_store, endpoints
from .auth_handler import (
    SOURCE_CLI,
    SOURCE_CONFIG,
    SOURCE_ENV,
    SOURCE_KEYRING,
    SOURCE_NETRC,
    get_api_key,
    keys_by_source,
)
from .config_manager import save_api_key_to_config
from .exceptions import APIRequestError
from .logger_setup import rich_console
from .models import GlobalArgs
from .netrc_handler import remove_key_from_netrc

ENVS = ["prod", "uat", "sit", "local"]
ENV_CHOICE = click.Choice(ENVS, case_sensitive=False)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _days_left(expires_at: Optional[str]) -> Optional[int]:
    if not expires_at:
        return None
    try:
        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return int((exp - datetime.now(timezone.utc)).total_seconds() // 86400)
    except ValueError:
        return None


def _args_for(ctx: click.Context, env: str) -> GlobalArgs:
    """Arguments for ``env`` with the endpoint policy applied (trusted mode)."""
    base: GlobalArgs = ctx.obj
    args = base.model_copy(update={"env_cli": env})
    endpoints.enforce(args)
    return args


def _default_env(ctx: click.Context) -> str:
    return str(ctx.obj.config.default_env)


def _print_json(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, default=str))


async def fetch_upload_targets(
    global_args: GlobalArgs, api_key: str
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Calls ``GET /v2/me/upload-targets``. Returns (result_code, json_or_None)."""
    from .api_client import API_VERSION_PREFIX, ApiClient

    client = ApiClient(global_args.config, global_args, api_key)
    try:
        response = await client._request(
            "GET",
            f"{API_VERSION_PREFIX}/me/upload-targets",
            expected_status_codes=[200],
        )
        return "ok", response.json()
    except APIRequestError as e:
        if e.status_code in (401, 403):
            return "AUTH_INVALID", None
        if e.status_code == 404:
            return "SERVER_TOO_OLD", None
        return ("TIMEOUT" if "timed out" in str(e) else "NETWORK"), None
    except ValueError:
        return "SERVER_TOO_OLD", None
    finally:
        await client.close()


def sync_caller_metadata(host: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Stores the non-secret ``caller.key`` metadata for ``host``; returns it."""
    caller = data.get("caller") or {}
    key = caller.get("key") or {}
    fields: Dict[str, Any] = {
        "name": key.get("name"),
        "suffix": key.get("suffix"),
        "expires_at": key.get("expires_at"),
        "owner_name": caller.get("name"),
    }
    if any(v is not None for v in fields.values()):
        credential_store.update_metadata(host, **fields)
    return {k: v for k, v in fields.items() if v is not None}


@click.group("auth")
def auth_group() -> None:
    """Manages upload keys per environment (OS keyring)."""


@auth_group.command("set-key")
@click.option(
    "--env", "env", type=ENV_CHOICE, help="Environment (default: default_env)."
)
@click.option("--stdin", "from_stdin", is_flag=True, help="Read the key from stdin.")
@click.option("--key-id", help="Key id (metadata only).")
@click.option("--name", help="Key name (metadata only).")
@click.option("--expires-at", help="Expiry, ISO 8601 (metadata only).")
@click.option("--source", default="cli", show_default=True, help="Who stored it.")
@click.pass_context
def set_key_command(
    ctx: click.Context,
    env: Optional[str],
    from_stdin: bool,
    key_id: Optional[str],
    name: Optional[str],
    expires_at: Optional[str],
    source: str,
) -> None:
    """Stores an API key for one environment in the OS keyring."""
    env = (env or _default_env(ctx)).lower()
    host = credential_store.host_for_env(_args_for(ctx, env).config, env)
    if not host:
        raise click.UsageError(f"No API URL configured for environment '{env}'.")
    if from_stdin:
        key = sys.stdin.readline().strip()
    else:
        key = click.prompt(f"API key for {env} ({host})", hide_input=True).strip()
    if not key:
        raise click.UsageError("No key given.")
    if not re.match(credential_store.KEY_PATTERN, key):
        rich_console.print(
            "[yellow]Warning: the key doesn't look like a CUE key (cue_sk_…); storing it anyway.[/yellow]"
        )
    if not credential_store.set_key(host, key):
        rich_console.print(
            "[bold red]The OS keyring is not available on this system. "
            "Use 'cue-upload configure --key' to store the key in .netrc instead.[/bold red]"
        )
        sys.exit(1)
    credential_store.update_metadata(
        host,
        replace=True,
        key_id=key_id,
        name=name,
        suffix=key[-4:],
        created_at=now_iso(),
        expires_at=expires_at,
        source=source,
    )
    rich_console.print(
        f"[green]API key for {env} ({host}) saved to the OS keyring.[/green]"
    )


def env_status(ctx: click.Context, env: str) -> Dict[str, Any]:
    args = _args_for(ctx, env)
    config = args.config
    host = credential_store.host_for_env(config, env)
    key, source = get_api_key(ctx.obj.api_key_cli, config, args, warn_shared=False)
    meta = credential_store.load_metadata().get(host or "", {}) if host else {}
    stored = keys_by_source(config, env)
    warnings: List[str] = []
    if source == SOURCE_KEYRING:
        for other in (SOURCE_NETRC, SOURCE_CONFIG):
            if stored[other]:
                label = ".netrc" if other == SOURCE_NETRC else "config.toml"
                warnings.append(f"An older key also exists in {label}; remove it?")
    elif source == SOURCE_NETRC and stored[SOURCE_CONFIG]:
        warnings.append("An older key also exists in config.toml; remove it?")
    if source == SOURCE_CONFIG:
        warnings.append(
            "Using the shared config.toml key; store a per-environment key with "
            f"'cue-upload auth set-key --env {env}'."
        )
    from_vault = source == SOURCE_KEYRING
    expires_at = meta.get("expires_at") if from_vault else None
    return {
        "env": env,
        "host": host,
        "present": key is not None,
        "source": source,
        "suffix": (meta.get("suffix") if from_vault else None)
        or (key[-4:] if key else None),
        "name": meta.get("name") if from_vault else None,
        "key_id": meta.get("key_id") if from_vault else None,
        "owner_name": meta.get("owner_name") if from_vault else None,
        "stored_by": meta.get("source") if from_vault else None,
        "expires_at": expires_at,
        "days_left": _days_left(expires_at),
        "warnings": warnings,
    }


@auth_group.command("status")
@click.option("--env", "env", type=ENV_CHOICE, help="One environment (default: all).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def status_command(ctx: click.Context, env: Optional[str], as_json: bool) -> None:
    """Shows which key each environment would use (never prints the key)."""
    envs = [env.lower()] if env else ENVS
    if not env and (ctx.obj.trusted_endpoints or endpoints.trusted_from_env()):
        if not ctx.obj.allow_local:
            envs = [e for e in envs if e != "local"]
    result = {
        "keyring_available": credential_store.available(),
        "keyring_backend": credential_store.backend_name(),
        "environments": {e: env_status(ctx, e) for e in envs},
    }
    if as_json:
        _print_json(result)
        return
    rich_console.print(
        f"OS keyring: [bold]{'available' if result['keyring_available'] else 'not available'}[/bold] "
        f"[dim]({result['keyring_backend']})[/dim]"
    )
    for e, st in result["environments"].items():  # type: ignore[attr-defined]
        if not st["present"]:
            rich_console.print(f"  {e:<5} {st['host']}: [yellow]no key[/yellow]")
            continue
        extra = f", expires {st['expires_at']}" if st["expires_at"] else ""
        if st["days_left"] is not None:
            extra += f" ({st['days_left']} day(s) left)"
        rich_console.print(
            f"  {e:<5} {st['host']}: key …{st['suffix']} from [bold]{st['source']}[/bold]{extra}"
        )
        for w in st["warnings"]:
            rich_console.print(f"        [yellow]{w}[/yellow]")


@auth_group.command("delete-key")
@click.option(
    "--env", "env", type=ENV_CHOICE, help="Environment (default: default_env)."
)
@click.option(
    "--all-sources", is_flag=True, help="Also remove it from .netrc and config.toml."
)
@click.option("--yes", is_flag=True, help="Don't ask for confirmation.")
@click.pass_context
def delete_key_command(
    ctx: click.Context, env: Optional[str], all_sources: bool, yes: bool
) -> None:
    """Removes the stored key for one environment."""
    env = (env or _default_env(ctx)).lower()
    host = credential_store.host_for_env(_args_for(ctx, env).config, env)
    if not host:
        raise click.UsageError(f"No API URL configured for environment '{env}'.")
    removed = credential_store.delete_key(host)
    credential_store.remove_metadata(host)
    rich_console.print(
        f"[green]Removed the OS keyring key for {env} ({host}).[/green]"
        if removed
        else f"[yellow]No OS keyring key for {env} ({host}).[/yellow]"
    )
    if not all_sources:
        return
    if not yes and not click.confirm(
        f"Also remove the {host} key from .netrc and the shared config.toml key?",
        default=False,
    ):
        return
    if remove_key_from_netrc(host):
        rich_console.print(f"[green]Removed the .netrc key for {host}.[/green]")
    if ctx.obj.config.api_key:
        save_api_key_to_config(None, ctx.obj.config_path_override)
        rich_console.print("[green]Removed the config.toml key.[/green]")


@auth_group.command("test")
@click.option(
    "--env", "env", type=ENV_CHOICE, help="Environment (default: default_env)."
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def test_command(ctx: click.Context, env: Optional[str], as_json: bool) -> None:
    """Checks that the key works with one quick call (never prints targets)."""
    env = (env or _default_env(ctx)).lower()
    args = _args_for(ctx, env)
    host = credential_store.host_for_env(args.config, env)
    key, source = get_api_key(ctx.obj.api_key_cli, args.config, args)
    out: Dict[str, Any] = {"env": env, "host": host}
    if not key:
        out["result"] = "AUTH_MISSING"
        code = 3
    else:
        result, data = asyncio.run(fetch_upload_targets(args, key))
        out["result"] = result
        if (
            result == "ok"
            and data is not None
            and host
            and source not in (SOURCE_CLI, SOURCE_ENV)
        ):
            out.update(sync_caller_metadata(host, data))
        code = {"ok": 0, "AUTH_INVALID": 3, "SERVER_TOO_OLD": 4}.get(result, 1)
    if as_json:
        _print_json(out)
    elif out["result"] == "ok":
        rich_console.print(f"[green]The key for {env} ({host}) works.[/green]")
    else:
        rich_console.print(f"[red]Key check for {env} ({host}): {out['result']}[/red]")
    if code:
        sys.exit(code)
