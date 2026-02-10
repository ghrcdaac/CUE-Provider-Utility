import asyncio
import click
import sys
from pathlib import Path
from typing import Optional, Any
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
from .uploader import process_upload
from .models import GlobalArgs
from .exceptions import CUEProviderError, UploadCancelledError, AuthError


@click.group(context_settings=dict(help_option_names=['-h', '--help']))
@click.version_option(package_name='CUE-Provider-Utility')
@click.option(
    '-t', '--token', 'api_key_cli',
    metavar='TEXT',
    envvar="CUE_UPLOAD_API_TOKEN",
    help="Authentication API key. Overrides all other sources."
)
@click.option(
    '--env',
    type=click.Choice(['prod', 'uat', 'sit', 'local'], case_sensitive=False),
    help="Target backend environment. Overrides default_env in config."
)
@click.option(
    '--config', 'config_path_override',
    type=click.Path(dir_okay=False, path_type=Path),
    help=f"Path to a custom configuration file. Default: {get_config_file_path()}"
)
@click.option(
    '--log-file', 'log_file_override',
    type=click.Path(path_type=Path),
    help=f"Path to a custom log file or directory. Default: {get_log_file_path().parent}"
)
@click.option(
    '-v', '--verbose',
    count=True,
    help="Increase console output verbosity (-v for INFO, -vv for DEBUG with full network logs)."
)
@click.option(
    '-q', '--quiet',
    is_flag=True,
    help="Minimal console output, showing only critical errors or final summaries."
)
@click.pass_context
def cli_app(
    ctx: click.Context,
    api_key_cli: Optional[str],
    env: Optional[str],
    config_path_override: Optional[Path],
    log_file_override: Optional[Path],
    verbose: int,
    quiet: bool,
):
    """CUE Provider Utility: CLI for uploading files and folders to CUE."""
    ctx.obj = GlobalArgs(
        api_key_cli=api_key_cli,
        env_cli=env,
        config_path_override=config_path_override,
        log_file_override=log_file_override,
        verbose_level=verbose,
        quiet_mode=quiet,
    )

    log_level = "ERROR" if quiet else "DEBUG" if verbose >= 2 else "INFO"
    
    final_log_path = log_file_override or get_log_file_path(config_path_override)
    if log_file_override and log_file_override.is_dir():
        final_log_path = log_file_override / get_log_file_path(config_path_override).name

    setup_logging(log_path=final_log_path, console_log_level=log_level.upper())

    try:
        cfg = get_config(config_path_override)
        ctx.obj.config = cfg
        if not env and cfg.default_env:
            ctx.obj.env_cli = cfg.default_env
    except CUEProviderError as e:
        rich_console.print(f"[bold red]Configuration error: {e}[/bold red]")
        sys.exit(1)


@cli_app.command("upload")
@click.option('-P', '--path', 'upload_path', type=click.Path(exists=True, readable=True, path_type=Path), required=True, help="Local file or directory path to be uploaded.")
@click.option('-c', '--collection', type=str, required=True, help="Name of the target collection in the backend system.")
@click.option('-tp', '--target-path', type=str, help="User-defined sub-path within the collection for organization.")
@click.option('-fc', '--file-concurrency', type=click.IntRange(min=1), help="Max number of files to upload concurrently (folder uploads).")
@click.option('-pc', '--part-concurrency', type=click.IntRange(min=1), help="Max concurrent parts for a single multipart file.")
@click.option('-y', '--auto-approve', is_flag=True, help="Skip confirmation prompt for folder uploads.")
@click.pass_context
def upload_command(ctx: click.Context, upload_path: Path, collection: str, target_path: Optional[str], file_concurrency: Optional[int], part_concurrency: Optional[int], auto_approve: bool):
    """Uploads files or directories to CUE."""
    global_args: GlobalArgs = ctx.obj
    config = global_args.config

    api_key, key_source = get_api_key(global_args.api_key_cli, config, global_args)
    if not api_key:
        rich_console.print("[bold red]API Key not found. Please configure it using 'cue-upload configure' or use the --token option.[/bold red]")
        sys.exit(1)

    if key_source and not global_args.quiet_mode:
        rich_console.print(f"[info]Using API key from [bold]{key_source}[/bold]...[/info]")

    rich_console.print(f"\nInitiating upload for: [bold cyan]{upload_path}[/bold cyan]")
    rich_console.print(f"Target Collection: [bold cyan]{collection}[/bold cyan]")
    if target_path:
        rich_console.print(f"Target Path: [bold cyan]{target_path}[/bold cyan]")

    try:
        asyncio.run(process_upload(
            source_path=upload_path,
            collection=collection,
            target_sub_path=target_path,
            api_key=api_key,
            config=config,
            global_args=global_args,
            file_concurrency=file_concurrency or config.file_concurrency,
            part_concurrency=part_concurrency or config.part_concurrency,
            auto_approve=auto_approve
        ))
        rich_console.print("\n[bold green]✅ Upload process finished successfully.[/bold green]")
    except UploadCancelledError as e:
        rich_console.print(f"\n[yellow]{e}[/yellow]")
        sys.exit(0)
    except (CUEProviderError, AuthError) as e:
        rich_console.print(f"\n[bold red]Error: {e}[/bold red]")
        if global_args.verbose_level < 2:
            rich_console.print("[dim]Hint: For more details, run with the -vv flag.[/dim]")
        sys.exit(1)
    except Exception as e:
        rich_console.print(f"\n[bold red]An unexpected critical error occurred: {e}[/bold red]")
        logging.getLogger(__name__).exception("Unexpected critical error in upload command")
        sys.exit(1)


@cli_app.command("configure")
@click.option('--key', 'set_key_only', is_flag=True, help="Only prompt for and save the API key.")
@click.pass_context
def configure_command(ctx: click.Context, set_key_only: bool):
    """Interactively configures CUE Provider Utility settings."""
    global_args: GlobalArgs = ctx.obj
    config = get_config(global_args.config_path_override)
    config_file = get_config_file_path(global_args.config_path_override)

    # --- Helper function for the API Key prompt logic ---
    def _handle_api_key_prompt():
        """Handles the logic for prompting and saving the API key."""
        nonlocal config # Allows modification of the config object from the outer scope
        rich_console.print("\n[bold]API Key Storage[/bold]")

        save_to_netrc_prompt = "Save API key to the secure .netrc file? (Recommended). If 'no', it will be saved to config.toml."
        save_to_netrc = click.confirm(save_to_netrc_prompt, default=config.save_key_to_netrc)
        if save_to_netrc != config.save_key_to_netrc:
            save_config_value("save_key_to_netrc", save_to_netrc, global_args.config_path_override)
            setattr(config, "save_key_to_netrc", save_to_netrc)

        key_location = ".netrc" if config.save_key_to_netrc else "config.toml"
        
        hostname = None
        if config.save_key_to_netrc:
            try:
                env = config.default_env
                base_url_obj = getattr(config.environments, env)
                hostname = urlparse(str(base_url_obj)).hostname
            except Exception:
                rich_console.print(f"[red]Could not determine hostname for environment '{config.default_env}'. Cannot manage .netrc key.[/red]")
                
        key_exists = False
        if hostname and config.save_key_to_netrc:
            key_exists = get_key_from_netrc(hostname) is not None
        else:
            key_exists = get_config(global_args.config_path_override).api_key is not None

        if key_exists:
            rich_console.print(f"\nAn API key is already set in [bold]{key_location}[/bold].")
            prompt_msg = "Enter a new key to overwrite, type [bold red]DELETE[/bold red] to remove, or press Enter to skip:"
        else:
            prompt_msg = f"Enter new API key to save to [bold]{key_location}[/bold] (input is visible, press Enter to skip):"

        rich_console.print(prompt_msg)
        new_key = click.prompt(">", default="", show_default=False, prompt_suffix="")

        if new_key.upper() == 'DELETE':
            if hostname and config.save_key_to_netrc:
                if remove_key_from_netrc(hostname):
                    rich_console.print(f"[yellow]API key for {hostname} removed from .netrc.[/yellow]")
            else:
                save_api_key_to_config(None, global_args.config_path_override)
                rich_console.print("[yellow]API key removed from config.toml.[/yellow]")
        elif new_key:
            if hostname and config.save_key_to_netrc:
                save_key_to_netrc(hostname, "cue-cli-user", new_key)
                rich_console.print(f"[green]API key for {hostname} saved to .netrc.[/green]")
            else:
                save_api_key_to_config(new_key, global_args.config_path_override)
                rich_console.print("[green]API key saved to config.toml.[/green]")
        else:
             # Only show this message if a key wasn't changed or deleted
            if not new_key.upper() == 'DELETE':
                rich_console.print("[yellow]API key not changed.[/yellow]")
    
    # --- Main command logic ---

    # If --key flag is used, only handle the API key and exit.
    if set_key_only:
        _handle_api_key_prompt()
        return

    # Otherwise, run the full interactive wizard.
    rich_console.print(f"\n[bold]CUE Provider Utility Configuration[/bold]")
    rich_console.print(f"Editing settings in: [dim]{config_file}[/dim]\n")

    # --- General Settings ---
    settings_to_configure = {
        "default_env": {"prompt": "Default backend environment", "type": click.Choice(['prod', 'uat', 'sit', 'local'])},
        "retry_attempts": {"prompt": "Network retry attempts on failure", "type": click.IntRange(0, 5)},
        "file_concurrency": {"prompt": "Default concurrent file uploads", "type": click.IntRange(1, 16)},
        "part_concurrency": {"prompt": "Default concurrent part uploads", "type": click.IntRange(1, 16)},
    }

    for key, details in settings_to_configure.items():
        current_value = getattr(config, key)
        setting_type = details["type"]
        
        prompt_parts = [f"{details['prompt']}:"]
        if isinstance(setting_type, click.Choice):
            prompt_parts.append(f"[dim](options: {', '.join(setting_type.choices)})[/dim]")
        elif isinstance(setting_type, click.IntRange):
            prompt_parts.append(f"[dim](range: {setting_type.min}-{setting_type.max})[/dim]")
        
        rich_console.print(" ".join(prompt_parts))
        
        while True:
            try:
                prompt_indicator = f"  (current: {current_value}) > "
                value_str = click.prompt(prompt_indicator, default=str(current_value), show_default=False)
                new_value = current_value if value_str == str(current_value) else setting_type.convert(value_str, None, None)
                
                if new_value != current_value:
                    save_config_value(key, new_value, global_args.config_path_override)
                    setattr(config, key, new_value)
                break
            except click.exceptions.BadParameter as e:
                rich_console.print(f"  [red]Invalid value: {e}. Please try again.[/red]")
        rich_console.print("-" * 20)

    # --- API Key Settings handled last ---
    _handle_api_key_prompt()

    rich_console.print("\n[green]Configuration finished.[/green]")


@cli_app.group("ignore")
def ignore_group():
    """Manages ignored file patterns for folder uploads."""
    pass

@ignore_group.command("list")
@click.pass_context
def ignore_list_command(ctx: click.Context):
    """Displays active system default and user-defined ignored file patterns."""
    global_args: GlobalArgs = ctx.obj
    system_patterns, user_patterns = list_ignore_patterns(global_args.config_path_override)
    rich_console.print("[bold cyan]System Default Ignore Patterns:[/bold cyan]")
    for p in system_patterns: rich_console.print(f"- {p}")
    rich_console.print("\n[bold cyan]User-Defined Ignore Patterns:[/bold cyan]")
    if user_patterns:
        for p in user_patterns: rich_console.print(f"- {p}")
    else:
        rich_console.print("  (None defined by user)")

@ignore_group.command("add")
@click.argument("pattern", type=str)
@click.pass_context
def ignore_add_command(ctx: click.Context, pattern: str):
    """Adds a new glob pattern to the user's ignore list."""
    global_args: GlobalArgs = ctx.obj
    try:
        if add_user_ignore_pattern(pattern, global_args.config_path_override):
            rich_console.print(f"[green]Pattern '{pattern}' added.[/green]")
            # Tip is added here
            rich_console.print("[dim]Tip: Use wildcards for broad matches (e.g., '*.log' to ignore all log files).[/dim]")
        else:
            rich_console.print(f"[yellow]Pattern '{pattern}' already exists.[/yellow]")
    except CUEProviderError as e:
        rich_console.print(f"[red]Error: {e}[/red]")

@ignore_group.command("remove")
@click.argument("pattern", type=str)
@click.pass_context
def ignore_remove_command(ctx: click.Context, pattern: str):
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
@click.confirmation_option(prompt="Are you sure you want to clear all user-defined ignore patterns?")
@click.pass_context
def ignore_reset_command(ctx: click.Context):
    """Clears all patterns from the user's ignore list."""
    global_args: GlobalArgs = ctx.obj
    try:
        reset_user_ignore_patterns(global_args.config_path_override)
        rich_console.print("[green]User-defined ignore patterns cleared.[/green]")
    except CUEProviderError as e:
        rich_console.print(f"[red]Error: {e}[/red]")

@cli_app.command("logs")
@click.option("-n", "--lines", type=int, help="Show the last N lines of the log file.")
@click.option("--raw", is_flag=True, help="Display raw log content without special formatting.")
@click.pass_context
def logs_command(ctx: click.Context, lines: Optional[int], raw: bool):
    """Views application logs directly in the terminal."""
    global_args: GlobalArgs = ctx.obj
    log_file_to_view = global_args.log_file_override or get_log_file_path(global_args.config_path_override)
    if not log_file_to_view.exists():
        rich_console.print(f"[red]Log file not found at: {log_file_to_view}[/red]")
        return
    rich_console.print(f"Displaying logs from: [cyan]{log_file_to_view}[/cyan]")
    asyncio.run(view_log_file(log_file_to_view, lines, False, raw))

if __name__ == '__main__':
    cli_app()

