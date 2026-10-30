import asyncio
import os
import json
import click
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, cast
from urllib.parse import urlparse
import logging

from .config_manager import (
    get_config,
    get_config_file_path,
    save_config_value,
    save_api_key_to_config,
)
from .logger_setup import setup_logging, get_log_file_path, view_log_file, rich_console
from .auth_handler import get_api_key
from .netrc_handler import save_key_to_netrc, get_key_from_netrc, remove_key_from_netrc
from .ignored_files_handler import (
    add_user_ignore_pattern,
    remove_user_ignore_pattern,
    list_ignore_patterns,
    reset_user_ignore_patterns,
)
from .uploader import UploadItem, process_upload, process_upload_items
from . import __version__
from .models import GlobalArgs, Manifest
from .exceptions import (
    CUEProviderError,
    UploadCancelledError,
    AuthError,
    UploadError,
    ConfigError,
)
from . import control, credential_store, endpoints, events, messages
from .endpoints import trusted_options
from .gui_commands import gui_group
from .auth_commands import auth_group, now_iso as _now_iso


# Friendly, fixed order in `cue-upload --help`; anything else is listed after.
COMMAND_ORDER = ["upload", "retry", "auth", "configure", "ignore", "logs"]
HELP_EPILOG = """\b
Examples:
  cue-upload upload -P ./data -c my_collection
  cue-upload retry
  cue-upload configure"""


class OrderedGroup(click.Group):
    def list_commands(self, ctx: click.Context) -> List[str]:
        def key(name: str) -> Tuple[int, str]:
            pos = COMMAND_ORDER.index(name) if name in COMMAND_ORDER else 99
            return pos, name

        return sorted(self.commands, key=key)


@click.group(
    cls=OrderedGroup,
    context_settings=dict(help_option_names=["-h", "--help"]),
    epilog=HELP_EPILOG,
)
@click.version_option(__version__, package_name="CUE-Provider-Utility")
@click.option(
    "-t",
    "--token",
    "api_key_cli",
    metavar="TEXT",
    envvar="CUE_UPLOAD_API_TOKEN",
    help="Authentication API key. Overrides all other sources.",
)
@click.option(
    "--env",
    type=click.Choice(["prod", "uat", "sit", "local"], case_sensitive=False),
    help="Target backend environment. Overrides default_env in config.",
)
@click.option(
    "--config",
    "config_path_override",
    type=click.Path(dir_okay=False, path_type=Path),
    help=f"Path to a custom configuration file. Default: {get_config_file_path()}",
)
@click.option(
    "--log-file",
    "log_file_override",
    type=click.Path(path_type=Path),
    help=f"Path to a custom log file or directory. Default: {get_log_file_path().parent}",
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase console output verbosity (-v for INFO, -vv for DEBUG with full network logs).",
)
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    help="Minimal console output, showing only critical errors or final summaries.",
)
@trusted_options
@click.pass_context
def cli_app(
    ctx: click.Context,
    trusted_endpoints: bool,
    allow_local: bool,
    api_key_cli: Optional[str],
    env: Optional[str],
    config_path_override: Optional[Path],
    log_file_override: Optional[Path],
    verbose: int,
    quiet: bool,
) -> None:
    """CUE Provider Utility: CLI for uploading files and folders to CUE."""
    ctx.obj = GlobalArgs(
        api_key_cli=api_key_cli,
        env_cli=env,
        config_path_override=config_path_override,
        log_file_override=log_file_override,
        verbose_level=verbose,
        quiet_mode=quiet,
        trusted_endpoints=trusted_endpoints,
        allow_local=allow_local,
    )

    log_level = "ERROR" if quiet else "DEBUG" if verbose >= 2 else "INFO"

    final_log_path = log_file_override or get_log_file_path(config_path_override)
    if log_file_override and log_file_override.is_dir():
        final_log_path = (
            log_file_override / get_log_file_path(config_path_override).name
        )

    setup_logging(log_path=final_log_path, console_log_level=log_level.upper())

    try:
        cfg = get_config(config_path_override)
        ctx.obj.config = cfg
        if not env and cfg.default_env:
            ctx.obj.env_cli = cfg.default_env
    except CUEProviderError as e:
        rich_console.print(f"[bold red]Configuration error: {e}[/bold red]")
        sys.exit(1)


def add_global_options(f: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to add global options (-t, --env, --config, --log-file, -v, -q) to subcommands."""
    f = click.option(
        "-t",
        "--token",
        "api_key_cli_sub",
        metavar="TEXT",
        envvar="CUE_UPLOAD_API_TOKEN",
        help="Authentication API key. Overrides all other sources.",
    )(f)
    f = click.option(
        "--env",
        "env_sub",
        type=click.Choice(["prod", "uat", "sit", "local"], case_sensitive=False),
        help="Target backend environment. Overrides default_env in config.",
    )(f)
    f = click.option(
        "--config",
        "config_path_override_sub",
        type=click.Path(dir_okay=False, path_type=Path),
        help="Path to a custom configuration file.",
    )(f)
    f = click.option(
        "--log-file",
        "log_file_override_sub",
        type=click.Path(path_type=Path),
        help="Path to a custom log file or directory.",
    )(f)
    f = click.option(
        "-v",
        "--verbose",
        "verbose_sub",
        count=True,
        help="Increase console output verbosity.",
    )(f)
    f = click.option(
        "-q", "--quiet", "quiet_sub", is_flag=True, help="Minimal console output."
    )(f)
    return f


def sync_global_options(
    ctx: click.Context,
    api_key_cli_sub: Optional[str] = None,
    env_sub: Optional[str] = None,
    config_path_override_sub: Optional[Path] = None,
    log_file_override_sub: Optional[Path] = None,
    verbose_sub: int = 0,
    quiet_sub: bool = False,
) -> None:
    """Synchronizes any subcommand-level global options into the ctx.obj GlobalArgs."""
    global_args: GlobalArgs = ctx.obj
    if api_key_cli_sub:
        global_args.api_key_cli = api_key_cli_sub
    if env_sub:
        global_args.env_cli = env_sub
    if config_path_override_sub:
        global_args.config_path_override = config_path_override_sub
    if log_file_override_sub:
        global_args.log_file_override = log_file_override_sub
    if verbose_sub > 0:
        global_args.verbose_level = max(global_args.verbose_level, verbose_sub)
    if quiet_sub:
        global_args.quiet_mode = quiet_sub

    if config_path_override_sub or env_sub:
        try:
            cfg = get_config(global_args.config_path_override)
            global_args.config = cfg
            if not global_args.env_cli and cfg.default_env:
                global_args.env_cli = cfg.default_env
        except CUEProviderError as e:
            rich_console.print(f"[bold red]Configuration error: {e}[/bold red]")
            sys.exit(1)

    if log_file_override_sub or verbose_sub > 0 or quiet_sub:
        log_level = (
            "ERROR"
            if global_args.quiet_mode
            else "DEBUG" if global_args.verbose_level >= 2 else "INFO"
        )
        final_log_path = global_args.log_file_override or get_log_file_path(
            global_args.config_path_override
        )
        if global_args.log_file_override and global_args.log_file_override.is_dir():
            final_log_path = (
                global_args.log_file_override
                / get_log_file_path(global_args.config_path_override).name
            )
        setup_logging(log_path=final_log_path, console_log_level=log_level.upper())


def json_events_option(f: Callable[..., Any]) -> Callable[..., Any]:
    """Hidden ``--json-events`` flag (NDJSON events on stdout, for the desktop app)."""
    return click.option(
        "--json-events",
        "json_events",
        is_flag=True,
        hidden=True,
        help="Emit newline-delimited JSON events on stdout (desktop app).",
    )(f)


def _apply_trusted(
    global_args: GlobalArgs, trusted_endpoints: bool, allow_local: bool
) -> None:
    """Merges subcommand-level flags and applies the endpoint policy."""
    global_args.trusted_endpoints = global_args.trusted_endpoints or trusted_endpoints
    global_args.allow_local = global_args.allow_local or allow_local
    endpoints.enforce(global_args)


def _start_events(json_events: bool, global_args: GlobalArgs) -> None:
    """Turns on events mode: stdout = NDJSON only, human console output muted."""
    if not json_events:
        return
    global_args.json_events = True
    events.start_session(events.JsonLinesSink())
    rich_console.quiet = True
    control.start()
    env = global_args.env_cli or global_args.config.default_env
    host = None
    try:
        host = urlparse(str(getattr(global_args.config.environments, env))).hostname
    except Exception:
        pass
    events.hello(env, host)


# Exit codes in --json-events mode (terminal mode keeps today's 0/1).
EXIT_CODES = {
    "ok": 0,
    "failed": 1,
    "usage": 2,
    "auth": 3,
    "cancelled": 130,
    "internal": 70,
}


def _exit(kind: str, terminal_code: int) -> None:
    """Exits with the events-mode code for ``kind`` or today's terminal code."""
    control.stop()
    sys.exit(EXIT_CODES[kind] if events.enabled() else terminal_code)


def _failure_kind(e: BaseException) -> str:
    if isinstance(e, AuthError) or events.current().auth_failed:
        return "auth"
    if isinstance(e, ConfigError):
        return "usage"
    return "failed"


def _finish_events(collection: Optional[str] = None, cancelled: bool = False) -> None:
    """Emits the batch notice and the final ``batch_done`` event (events mode only)."""
    if not events.enabled():
        return
    session = events.current()
    if cancelled:
        session.mark_cancelled()
    counts = session.counts()
    if cancelled:
        messages.report("CANCELLED")
    elif counts["failed"]:
        messages.report(
            "BATCH_PARTIAL", succeeded=counts["succeeded"], failed=counts["failed"]
        )
    elif counts["succeeded"]:
        messages.report(
            "BATCH_OK", succeeded=counts["succeeded"], collection=collection or ""
        )
    session.batch_done(cancelled=cancelled)


def _load_items(
    upload_paths: Tuple[Path, ...], manifest_path: Optional[Path]
) -> List[UploadItem]:
    """Builds the upload items from repeated ``-P`` or a ``--manifest`` file."""
    if not manifest_path:
        return [UploadItem(p) for p in upload_paths]
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = Manifest.model_validate(json.load(f))
    except (OSError, ValueError) as e:
        raise click.BadParameter(
            f"Invalid manifest: {str(e).splitlines()[0]}", param_hint="--manifest"
        )
    if not manifest.items:
        raise click.BadParameter("Manifest has no items.", param_hint="--manifest")
    return [
        UploadItem(Path(i.path).expanduser(), i.target_path, from_manifest=True)
        for i in manifest.items
    ]


@cli_app.command("upload")
@add_global_options
@click.option(
    "-P",
    "--path",
    "upload_paths",
    type=click.Path(exists=True, readable=True, path_type=Path),
    multiple=True,
    help="Local file or directory path to be uploaded (repeat for several).",
)
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    hidden=True,
    help='JSON file {"items":[{"path":"...","target_path":"..."}]} (desktop app).',
)
@click.option(
    "--non-interactive",
    "non_interactive",
    is_flag=True,
    hidden=True,
    help="Never prompt; fail instead of asking for confirmation.",
)
@click.option(
    "-c",
    "--collection",
    type=str,
    required=True,
    help="Name of the target collection in the backend system.",
)
@click.option(
    "-tp",
    "--target-path",
    type=str,
    help="User-defined sub-path within the collection for organization.",
)
@click.option(
    "-fc",
    "--file-concurrency",
    type=click.IntRange(min=1),
    help="Max number of files to upload concurrently (folder uploads).",
)
@click.option(
    "-pc",
    "--part-concurrency",
    type=click.IntRange(min=1),
    help="Max concurrent parts for a single multipart file.",
)
@click.option(
    "-y",
    "--auto-approve",
    is_flag=True,
    help="Skip confirmation prompt for folder uploads.",
)
@json_events_option
@trusted_options
@click.pass_context
def upload_command(
    ctx: click.Context,
    upload_paths: Tuple[Path, ...],
    manifest_path: Optional[Path],
    non_interactive: bool,
    collection: str,
    target_path: Optional[str],
    file_concurrency: Optional[int],
    part_concurrency: Optional[int],
    auto_approve: bool,
    json_events: bool,
    trusted_endpoints: bool,
    allow_local: bool,
    api_key_cli_sub: Optional[str],
    env_sub: Optional[str],
    config_path_override_sub: Optional[Path],
    log_file_override_sub: Optional[Path],
    verbose_sub: int,
    quiet_sub: bool,
) -> None:
    """Uploads files or directories to CUE."""
    sync_global_options(
        ctx,
        api_key_cli_sub,
        env_sub,
        config_path_override_sub,
        log_file_override_sub,
        verbose_sub,
        quiet_sub,
    )
    global_args: GlobalArgs = ctx.obj
    _apply_trusted(global_args, trusted_endpoints, allow_local)
    config = global_args.config
    if manifest_path and upload_paths:
        raise click.UsageError("Use either -P/--path or --manifest, not both.")
    if not manifest_path and not upload_paths:
        raise click.UsageError("Missing option '-P' / '--path'.")
    items = _load_items(upload_paths, manifest_path)
    global_args.non_interactive = non_interactive or json_events
    _start_events(json_events, global_args)

    api_key, key_source = get_api_key(global_args.api_key_cli, config, global_args)
    if not api_key:
        rich_console.print(
            "[bold red]API Key not found. Please configure it using 'cue-upload configure' or use the --token option.[/bold red]"
        )
        messages.report("AUTH_MISSING", env=global_args.env_cli or config.default_env)
        _finish_events(collection)
        _exit("auth", 1)
    single = items[0].path if len(items) == 1 and not manifest_path else None
    upload_label = str(single) if single else f"{len(items)} item(s)"

    if key_source and not global_args.quiet_mode:
        rich_console.print(
            f"[info]Using API key from [bold]{key_source}[/bold]...[/info]"
        )

    rich_console.print(
        f"\nInitiating upload for: [bold cyan]{upload_label}[/bold cyan]"
    )
    rich_console.print(f"Target Collection: [bold cyan]{collection}[/bold cyan]")
    if target_path:
        rich_console.print(f"Target Path: [bold cyan]{target_path}[/bold cyan]")

    common: Dict[str, Any] = dict(
        collection=collection,
        target_sub_path=target_path,
        api_key=api_key,
        config=config,
        global_args=global_args,
        file_concurrency=file_concurrency or config.file_concurrency,
        part_concurrency=part_concurrency or config.part_concurrency,
        auto_approve=auto_approve,
    )
    try:
        if single is not None:
            # One -P: exactly today's code path.
            work = process_upload(source_path=single, **common)
        else:
            work = process_upload_items(items=items, **common)
        asyncio.run(control.run_cancellable(work))
        rich_console.print(
            "\n[bold green]✅ Upload process finished successfully.[/bold green]"
        )
        _finish_events(collection)
        control.stop()
    except UploadCancelledError as e:
        rich_console.print(f"\n[yellow]{e}[/yellow]")
        _finish_events(collection, cancelled=True)
        _exit("cancelled", 0)
    except (CUEProviderError, AuthError) as e:
        rich_console.print(f"\n[bold red]Error: {e}[/bold red]")
        if not (isinstance(e, UploadError) and events.current().counts()["failed"]):
            messages.report_exception(e, name=upload_label)
        _finish_events(collection)
        if global_args.verbose_level < 2:
            rich_console.print(
                "[dim]Hint: For more details, run with the -vv flag.[/dim]"
            )
        _exit(_failure_kind(e), 1)
    except click.ClickException:
        _finish_events(collection)
        control.stop()
        raise
    except Exception as e:
        rich_console.print(
            f"\n[bold red]An unexpected critical error occurred: {e}[/bold red]"
        )
        logging.getLogger(__name__).exception(
            "Unexpected critical error in upload command"
        )
        messages.report("INTERNAL", detail=str(e))
        _finish_events(collection)
        _exit("internal", 1)


cli_app.add_command(auth_group)
cli_app.add_command(gui_group)


@cli_app.command("configure")
@click.option(
    "--key", "set_key_only", is_flag=True, help="Only prompt for and save the API key."
)
@click.pass_context
def configure_command(ctx: click.Context, set_key_only: bool) -> None:
    """Interactively configures CUE Provider Utility settings."""
    global_args: GlobalArgs = ctx.obj
    config = get_config(global_args.config_path_override)
    config_file = get_config_file_path(global_args.config_path_override)

    # --- Helper function for the API Key prompt logic ---
    def _handle_api_key_prompt() -> None:
        """Handles the logic for prompting and saving the API key."""
        rich_console.print("\n[bold]API Key Storage[/bold]")

        vault_ok = credential_store.available()
        if config.use_keyring and vault_ok:
            default_choice = 1
        else:
            default_choice = 2 if config.save_key_to_netrc else 3
        rich_console.print(
            "Store key in: [1] OS keyring (recommended) [2] .netrc [3] config.toml"
            + ("" if vault_ok else " [dim](OS keyring not available here)[/dim]")
        )
        choice = click.prompt(
            "Choice", type=click.IntRange(1, 3), default=default_choice
        )
        if choice == 1 and not vault_ok:
            rich_console.print(
                "[yellow]The OS keyring is not available; using .netrc instead.[/yellow]"
            )
            choice = 2
        updates = {"use_keyring": choice == 1}
        if choice != 1:  # .netrc vs config.toml keeps its old meaning
            updates["save_key_to_netrc"] = choice == 2
        for key, value in updates.items():
            if getattr(config, key) != value:
                save_config_value(key, value, global_args.config_path_override)
                setattr(config, key, value)

        key_location = {1: "the OS keyring", 2: ".netrc", 3: "config.toml"}[choice]

        hostname = None
        if choice in (1, 2):
            hostname = credential_store.host_for_env(config, config.default_env)
            if not hostname:
                rich_console.print(
                    f"[red]Could not determine hostname for environment '{config.default_env}'. Cannot manage {key_location} key.[/red]"
                )

        if choice == 1:
            key_exists = bool(hostname and credential_store.get_key(hostname))
        elif choice == 2:
            key_exists = bool(hostname and get_key_from_netrc(hostname) is not None)
        else:
            key_exists = (
                get_config(global_args.config_path_override).api_key is not None
            )

        if key_exists:
            rich_console.print(
                f"\nAn API key is already set in [bold]{key_location}[/bold]."
            )
            prompt_msg = "Enter a new key to overwrite, type [bold red]DELETE[/bold red] to remove, or press Enter to skip:"
        else:
            prompt_msg = f"Enter new API key to save to [bold]{key_location}[/bold] (input is hidden, press Enter to skip):"

        rich_console.print(prompt_msg)
        new_key = click.prompt(
            ">", default="", show_default=False, prompt_suffix="", hide_input=True
        ).strip()

        if new_key.upper() == "DELETE":
            if choice == 1 and hostname:
                if credential_store.delete_key(hostname):
                    credential_store.remove_metadata(hostname)
                    rich_console.print(
                        f"[yellow]API key for {hostname} removed from the OS keyring.[/yellow]"
                    )
            elif choice == 2 and hostname:
                if remove_key_from_netrc(hostname):
                    rich_console.print(
                        f"[yellow]API key for {hostname} removed from .netrc.[/yellow]"
                    )
            elif choice == 3:
                save_api_key_to_config(None, global_args.config_path_override)
                rich_console.print("[yellow]API key removed from config.toml.[/yellow]")
        elif new_key:
            if choice == 1 and hostname:
                if credential_store.set_key(hostname, new_key):
                    credential_store.update_metadata(
                        hostname,
                        replace=True,
                        suffix=new_key[-4:],
                        source="cli",
                        created_at=_now_iso(),
                    )
                    rich_console.print(
                        f"[green]API key for {hostname} saved to the OS keyring.[/green]"
                    )
                else:
                    rich_console.print(
                        "[red]Could not save to the OS keyring. Run 'cue-upload configure --key' and choose .netrc.[/red]"
                    )
            elif choice == 2 and hostname:
                save_key_to_netrc(hostname, "cue-cli-user", new_key)
                rich_console.print(
                    f"[green]API key for {hostname} saved to .netrc.[/green]"
                )
            elif choice == 3:
                save_api_key_to_config(new_key, global_args.config_path_override)
                rich_console.print("[green]API key saved to config.toml.[/green]")
        else:
            rich_console.print("[yellow]API key not changed.[/yellow]")

    # --- Main command logic ---

    # If --key flag is used, only handle the API key and exit.
    if set_key_only:
        _handle_api_key_prompt()
        return

    # Otherwise, run the full interactive wizard.
    rich_console.print("\n[bold]CUE Provider Utility Configuration[/bold]")
    rich_console.print(f"Editing settings in: [dim]{config_file}[/dim]\n")

    # --- General Settings ---
    settings_to_configure = {
        "default_env": {
            "prompt": "Default backend environment",
            "type": click.Choice(["prod", "uat", "sit", "local"], case_sensitive=False),
        },
        "retry_attempts": {
            "prompt": "Network retry attempts on failure",
            "type": click.IntRange(0, 5),
        },
        "auto_retry": {
            "prompt": "Enable automatic retry of failed uploads at end of session",
            "type": click.BOOL,
        },
        "failed_log_path": {
            "prompt": "Path for failed uploads log file",
            "type": click.Path(path_type=Path),
        },
        "file_concurrency": {
            "prompt": "Default concurrent file uploads",
            "type": click.IntRange(1, 16),
        },
        "part_concurrency": {
            "prompt": "Default concurrent part uploads",
            "type": click.IntRange(1, 16),
        },
    }

    for key, details in settings_to_configure.items():
        current_value = getattr(config, key)
        setting_type = details["type"]

        prompt_parts = [f"{details['prompt']}:"]
        if isinstance(setting_type, click.Choice):
            prompt_parts.append(
                f"[dim](options: {', '.join(setting_type.choices)})[/dim]"
            )
        elif isinstance(setting_type, click.IntRange):
            prompt_parts.append(
                f"[dim](range: {setting_type.min}-{setting_type.max})[/dim]"
            )
        elif setting_type == click.BOOL:
            prompt_parts.append("[dim](options: true/false or y/n)[/dim]")

        rich_console.print(" ".join(prompt_parts))

        while True:
            try:
                prompt_indicator = f"  (current: {current_value}) > "
                raw_input = click.prompt(
                    prompt_indicator, default=str(current_value), show_default=False
                )
                input_str = raw_input.strip().lower()

                if input_str == str(current_value).lower():
                    new_value = current_value
                elif setting_type == click.BOOL:
                    if input_str in ("true", "yes", "y", "1"):
                        new_value = True
                    elif input_str in ("false", "no", "n", "0"):
                        new_value = False
                    else:
                        raise ValueError("Please enter 'true', 'false', 'y', or 'n'.")
                elif isinstance(setting_type, click.Choice):
                    valid_choices = [c.lower() for c in setting_type.choices]
                    if input_str not in valid_choices:
                        raise ValueError(
                            f"Please select from: {', '.join(setting_type.choices)}"
                        )
                    new_value = input_str
                else:
                    new_value = cast(click.ParamType, setting_type).convert(
                        raw_input, None, None
                    )

                if new_value != current_value:
                    save_config_value(key, new_value, global_args.config_path_override)
                    setattr(config, key, new_value)
                break
            except Exception as e:
                err_msg = str(e)
                if isinstance(e, click.exceptions.BadParameter):
                    err_msg = e.format_message()
                rich_console.print(
                    f"  [red]Invalid value: {err_msg} Please try again.[/red]"
                )
        rich_console.print("-" * 20)

    # --- API Key Settings handled last ---
    _handle_api_key_prompt()

    rich_console.print("\n[green]Configuration finished.[/green]")


@cli_app.group("ignore")
def ignore_group() -> None:
    """Manages ignored file patterns for folder uploads."""
    pass


@ignore_group.command("list")
@click.pass_context
def ignore_list_command(ctx: click.Context) -> None:
    """Displays active system default and user-defined ignored file patterns."""
    global_args: GlobalArgs = ctx.obj
    system_patterns, user_patterns = list_ignore_patterns(
        global_args.config_path_override
    )
    rich_console.print("[bold cyan]System Default Ignore Patterns:[/bold cyan]")
    for p in system_patterns:
        rich_console.print(f"- {p}")
    rich_console.print("\n[bold cyan]User-Defined Ignore Patterns:[/bold cyan]")
    if user_patterns:
        for p in user_patterns:
            rich_console.print(f"- {p}")
    else:
        rich_console.print("  (None defined by user)")


@ignore_group.command("add")
@click.argument("pattern", type=str)
@click.pass_context
def ignore_add_command(ctx: click.Context, pattern: str) -> None:
    """Adds a new glob pattern to the user's ignore list."""
    global_args: GlobalArgs = ctx.obj
    try:
        if add_user_ignore_pattern(pattern, global_args.config_path_override):
            rich_console.print(f"[green]Pattern '{pattern}' added.[/green]")
            rich_console.print(
                "[dim]Tip: Use wildcards for broad matches (e.g., '*.log' to ignore all log files).[/dim]"
            )
        else:
            rich_console.print(f"[yellow]Pattern '{pattern}' already exists.[/yellow]")
    except CUEProviderError as e:
        rich_console.print(f"[red]Error: {e}[/red]")


@ignore_group.command("remove")
@click.argument("pattern", type=str)
@click.pass_context
def ignore_remove_command(ctx: click.Context, pattern: str) -> None:
    """Removes a glob pattern from the user's ignore list."""
    global_args: GlobalArgs = ctx.obj
    try:
        if remove_user_ignore_pattern(pattern, global_args.config_path_override):
            rich_console.print(f"[green]Pattern '{pattern}' removed.[/green]")
        else:
            rich_console.print(f"[yellow]Pattern '{pattern}' not found.[/yellow]")
    except CUEProviderError as e:
        rich_console.print(f"[red]Error: {e}[/red]")


@ignore_group.command("reset")
@click.confirmation_option(
    prompt="Are you sure you want to clear all user-defined ignore patterns?"
)
@click.pass_context
def ignore_reset_command(ctx: click.Context) -> None:
    """Clears all patterns from the user's ignore list."""
    global_args: GlobalArgs = ctx.obj
    try:
        reset_user_ignore_patterns(global_args.config_path_override)
        rich_console.print("[green]User-defined ignore patterns cleared.[/green]")
    except CUEProviderError as e:
        rich_console.print(f"[red]Error: {e}[/red]")


@cli_app.command("logs")
@click.option("-n", "--lines", type=int, help="Show the last N lines of the log file.")
@click.option(
    "--raw", is_flag=True, help="Display raw log content without special formatting."
)
@click.pass_context
def logs_command(ctx: click.Context, lines: Optional[int], raw: bool) -> None:
    """Views application logs directly in the terminal."""
    global_args: GlobalArgs = ctx.obj
    log_file_to_view = global_args.log_file_override or get_log_file_path(
        global_args.config_path_override
    )
    if not log_file_to_view.exists():
        rich_console.print(f"[red]Log file not found at: {log_file_to_view}[/red]")
        return
    rich_console.print(f"Displaying logs from: [cyan]{log_file_to_view}[/cyan]")
    asyncio.run(view_log_file(log_file_to_view, lines, False, raw))


@cli_app.command("retry")
@add_global_options
@click.option(
    "-f",
    "--failed-log",
    "failed_log_override",
    type=click.Path(path_type=Path),
    help="Path to a custom failed uploads log file.",
)
@click.option(
    "--entry-id",
    "entry_ids",
    multiple=True,
    help="Retry only this failed-upload entry (repeatable). Default: all entries.",
)
@click.option(
    "--non-interactive",
    "non_interactive",
    is_flag=True,
    hidden=True,
    help="Never prompt (retry never prompts; accepted for symmetry).",
)
@json_events_option
@trusted_options
@click.pass_context
def retry_command(
    ctx: click.Context,
    failed_log_override: Optional[Path],
    entry_ids: Tuple[str, ...],
    non_interactive: bool,
    json_events: bool,
    trusted_endpoints: bool,
    allow_local: bool,
    api_key_cli_sub: Optional[str],
    env_sub: Optional[str],
    config_path_override_sub: Optional[Path],
    log_file_override_sub: Optional[Path],
    verbose_sub: int,
    quiet_sub: bool,
) -> None:
    """Retries all failed upload entries saved in the failed uploads log file."""
    from .failed_uploads_handler import FailedUploadsManager
    from .uploader import retry_failed_entries

    sync_global_options(
        ctx,
        api_key_cli_sub,
        env_sub,
        config_path_override_sub,
        log_file_override_sub,
        verbose_sub,
        quiet_sub,
    )
    global_args: GlobalArgs = ctx.obj
    _apply_trusted(global_args, trusted_endpoints, allow_local)
    config = global_args.config
    _start_events(json_events, global_args)
    target_failed_log = failed_log_override or config.failed_log_path

    if not target_failed_log.exists():
        rich_console.print(
            f"[green]No failed uploads log file found at: {target_failed_log}[/green]"
        )
        _finish_events()
        return

    failed_entries = asyncio.run(
        FailedUploadsManager.get_failed_entries(target_failed_log)
    )

    if not failed_entries:
        rich_console.print(
            f"[green]No pending failed uploads found in log at {target_failed_log}.[/green]"
        )
        _finish_events()
        return

    rich_console.print(
        f"Found [bold cyan]{len(failed_entries)}[/bold cyan] failed upload item(s) in [dim]{target_failed_log}[/dim]"
    )

    if entry_ids:
        wanted = set(entry_ids)
        failed_entries = [e for e in failed_entries if e.entry_id in wanted]
        missing = wanted - {e.entry_id for e in failed_entries}
        if missing:
            rich_console.print(
                f"[yellow]Not in the failed uploads log: {', '.join(sorted(missing))}[/yellow]"
            )
        if not failed_entries:
            _finish_events()
            _exit("usage", 1)
            return

    try:
        succeeded, failed = asyncio.run(
            control.run_cancellable(
                retry_failed_entries(
                    entries=failed_entries,
                    config=config,
                    global_args=global_args,
                    failed_log_path=target_failed_log,
                    retry_rounds=max(1, config.retry_attempts),
                )
            )
        )
    except UploadCancelledError as e:
        rich_console.print(f"\n[yellow]{e}[/yellow]")
        _finish_events(cancelled=True)
        _exit("cancelled", 0)
        return

    _finish_events()
    if failed == 0:
        rich_console.print(
            "\n[bold green]✅ All failed uploads were retried and completed successfully![/bold green]"
        )
    else:
        rich_console.print(
            f"\n[bold yellow]Retry complete: {succeeded} succeeded, {failed} still failing.[/bold yellow]"
        )
        _exit("auth" if events.current().auth_failed else "failed", 1)
    control.stop()


_ORIGINAL_HIDDEN: Dict[int, bool] = {}


def apply_help_visibility(show_all: bool) -> None:
    """Shows hidden GUI plumbing in --help when ``show_all`` (support/developers).

    Set ``CUE_UPLOAD_SHOW_ALL_HELP=1`` to enable it from the shell.
    """

    def walk(cmd: click.Command) -> None:
        items: List[Any] = [cmd, *cmd.params]
        for item in items:
            if not hasattr(item, "hidden"):
                continue
            original = _ORIGINAL_HIDDEN.setdefault(id(item), bool(item.hidden))
            item.hidden = False if show_all else original
        if isinstance(cmd, click.Group):
            for sub in cmd.commands.values():
                walk(sub)

    walk(cli_app)


apply_help_visibility(os.environ.get("CUE_UPLOAD_SHOW_ALL_HELP") == "1")


if __name__ == "__main__":
    cli_app()
