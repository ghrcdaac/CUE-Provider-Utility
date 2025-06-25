"""
Main entry point for the CUE Provider Utility CLI.
Defines the main Click command group and registers subcommands.
"""
import asyncio
import click
import sys
from pathlib import Path
from typing import Optional 

from .config_manager import (
    ensure_config_exists,
    get_config,
    get_config_file_path,
    display_config_value_for_edit,
    save_config_value,
    save_api_token_to_config,
)
from .logger_setup import setup_logging, get_log_file_path, view_log_file
from .auth_handler import get_auth_token
from .ignored_files_handler import (
    add_user_ignore_pattern,
    remove_user_ignore_pattern,
    list_ignore_patterns,
    reset_user_ignore_patterns,
)
from .uploader import process_upload 
from .models import GlobalArgs 
from .exceptions import CUEProviderError, UploadCancelledError


# Ensure configuration and log directories exist on import/startup
ensure_config_exists()
LOG_FILE_PATH = get_log_file_path()
setup_logging(log_path=LOG_FILE_PATH)


@click.group(context_settings=dict(help_option_names=['-h', '--help']))
@click.version_option(package_name='CUE-Provider-Utility')
@click.option(
    '-t', '--token',
    metavar='TEXT',
    envvar="CUE_UPLOAD_API_TOKEN",
    help="Authentication token (JWT). Overrides token in config file or .netrc."
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
    help=f"Path to a custom log file or directory. Default: {LOG_FILE_PATH.parent if LOG_FILE_PATH else '~/.cue-upload/logs/'}"
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
    token: Optional[str], 
    env: Optional[str], 
    config_path_override: Optional[Path], 
    log_file_override: Optional[Path], 
    verbose: int,
    quiet: bool,
):
    """
    CUE Provider Utility: CLI for uploading files and folders to CUE.
    """
    ctx.obj = GlobalArgs(
        token_cli=token,
        env_cli=env,
        config_path_override=config_path_override,
        log_file_override=log_file_override,
        verbose_level=verbose,
        quiet_mode=quiet,
    )
    
    log_level = "INFO"
    if quiet:
        log_level = "ERROR"
    elif verbose == 1:
        log_level = "INFO"
    elif verbose >= 2:
        log_level = "DEBUG"

    final_log_path = log_file_override or LOG_FILE_PATH
    if log_file_override and log_file_override.is_dir():
        final_log_path = log_file_override / LOG_FILE_PATH.name

    setup_logging(log_path=final_log_path, console_log_level=log_level.upper())

    try:
        cfg = get_config(config_path_override)
        ctx.obj.config = cfg
        if not env and cfg.default_env:
            ctx.obj.env_cli = cfg.default_env
    except CUEProviderError as e:
        click.secho(f"Configuration error: {e}", fg="red", err=True)
        sys.exit(1)


@cli_app.command("upload")
@click.option(
    '-P', '--path', 'upload_path',
    type=click.Path(exists=True, readable=True, path_type=Path),
    required=True,
    help="Local file or directory path to be uploaded."
)
@click.option(
    '-c', '--collection',
    type=str,
    required=True,
    help="Name of the target collection in the backend system."
)
@click.option(
    '-tp', '--target-path',
    type=str,
    help="User-defined sub-path within the collection for organization."
)
@click.option(
    '-fc', '--file-concurrency',
    type=click.IntRange(min=1),
    help="Max number of files to upload concurrently (folder uploads)."
)
@click.option(
    '-pc', '--part-concurrency',
    type=click.IntRange(min=1),
    help="Max concurrent parts for a single multipart file."
)
@click.option(
    '-y', '--auto-approve',
    is_flag=True,
    help="Skip confirmation prompt for folder uploads."
)
@click.pass_context
def upload_command(
    ctx: click.Context,
    upload_path: Path,
    collection: str,
    target_path: Optional[str],
    file_concurrency: Optional[int], 
    part_concurrency: Optional[int], 
    auto_approve: bool,
):
    """Uploads files or directories to CUE."""
    global_args: GlobalArgs = ctx.obj
    config = global_args.config

    final_file_concurrency = file_concurrency or config.file_concurrency
    final_part_concurrency = part_concurrency or config.part_concurrency

    click.echo(f"Initiating upload for: {upload_path}")
    click.echo(f"Collection: {collection}")
    if target_path:
        click.echo(f"Target Path: {target_path}")
    
    try:
        selected_env = global_args.env_cli or config.default_env
        selected_env_url_obj = getattr(config.environments, selected_env, None)
        selected_env_url_str = str(selected_env_url_obj) if selected_env_url_obj else None

        auth_token = get_auth_token(global_args.token_cli, config, selected_env_url_str)
        if not auth_token:
            click.secho("Authentication token not found. Please configure using 'cue-upload configure --token' or use the --token option.", fg="red", err=True)
            sys.exit(1)

        async def run_upload():
            await process_upload(
                source_path=upload_path,
                collection=collection,
                target_sub_path=target_path,
                auth_token=auth_token,
                config=config,
                global_args=global_args,
                file_concurrency=final_file_concurrency,
                part_concurrency=final_part_concurrency,
                auto_approve=auto_approve
            )
            # This message is now only printed if the process completes without any exceptions.
            click.secho("\nUpload process finished successfully.", fg="green")

        asyncio.run(run_upload())
    
    # Catch the specific exceptions for better user feedback
    except UploadCancelledError as e:
        # Handle user cancellation gracefully
        click.secho(f"\n{e}", fg="yellow")
        sys.exit(0)
    except CUEProviderError as e:
        # Display the detailed error message propagated from the API client or other utils
        click.secho(f"\nError: {e}", fg="red", err=True)
        if global_args.verbose_level < 2:
            click.echo("Hint: For more details, run the command with the -vv flag.", err=True)
        sys.exit(1)
    except Exception as e:
        # Catch any other truly unexpected errors
        click.secho(f"\nAn unexpected critical error occurred: {e}", fg="red", err=True)
        import logging
        logging.exception("Unexpected critical error in upload command")
        sys.exit(1)


@cli_app.command("configure")
@click.option('--token', 'set_token_only', is_flag=True, help="Only prompt for and save the API token.")
@click.pass_context
def configure_command(ctx: click.Context, set_token_only: bool):
    """Interactively configures CUE Provider Utility settings."""
    global_args: GlobalArgs = ctx.obj
    config_file = get_config_file_path(global_args.config_path_override)
    click.echo(f"Configuring settings in: {config_file}")

    current_config = get_config(global_args.config_path_override)

    if set_token_only:
        new_token = click.prompt("Enter API Token (leave blank to keep current)", hide_input=True, default="", show_default=False)
        if new_token: 
            save_api_token_to_config(new_token, global_args.config_path_override)
            click.secho("API Token updated.", fg="green")
        else:
            click.echo("API Token not changed.")
        return
    
    settings_to_configure = {
        "default_env": {"prompt": "Default backend environment", "type": str, "choices": ['prod', 'uat', 'sit', 'local']},
        "retry_attempts": {"prompt": "Retry attempts for uploads (0-5)", "type": click.IntRange(0, 5)},
        "multipart_threshold_gb": {"prompt": "Multipart upload threshold (GB)", "type": int},
        "multipart_chunk_size_mb": {"prompt": "Multipart chunk size (MB)", "type": int},
        "log_level": {"prompt": "Log file verbosity", "type": str, "choices": ["DEBUG", "INFO", "WARNING", "ERROR"]},
        "file_concurrency": {"prompt": "Default file concurrency for folder uploads", "type": int},
        "part_concurrency": {"prompt": "Default part concurrency for multipart uploads", "type": int},
    }

    updated_values = {}
    for key, details in settings_to_configure.items():
        current_value = getattr(current_config, key, None)
        prompt_text = display_config_value_for_edit(details["prompt"], current_value)
        
        value_type = details["type"]
        choices = details.get("choices")

        if choices:
            new_value = click.prompt(prompt_text, type=click.Choice(choices, case_sensitive=False), default=str(current_value) if current_value is not None else None, show_default="current")
        else:
            new_value_str = click.prompt(prompt_text, default=str(current_value) if current_value is not None else "", show_default="current")
            if new_value_str == "" and current_value is not None: 
                new_value = current_value
            elif new_value_str == "" and current_value is None: 
                new_value = None 
            else:
                try:
                    if isinstance(value_type, click.ParamType):
                         new_value = value_type.convert(new_value_str, None, None)
                    else:
                         new_value = value_type(new_value_str)
                except (ValueError, click.exceptions.BadParameter) as e:
                    click.secho(f"Invalid value: {e}", fg="red")
                    continue
        
        if new_value != current_value and new_value is not None: 
            updated_values[key] = new_value

    if updated_values:
        for key, value in updated_values.items():
            save_config_value(key, value, global_args.config_path_override)
        click.secho("Configuration updated.", fg="green")
    else:
        click.echo("No configuration values were changed.")

    if not set_token_only:
        new_token = click.prompt("Enter API Token (press Enter to keep current, or type 'DELETE' to remove)", default="", show_default=False, hide_input=True)
        if new_token.upper() == 'DELETE':
            save_api_token_to_config(None, global_args.config_path_override) 
            click.secho("API Token removed from config.", fg="yellow")
        elif new_token: 
            save_api_token_to_config(new_token, global_args.config_path_override)
            click.secho("API Token updated.", fg="green")
        else: 
            click.echo("API Token not changed.")


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
    
    click.echo(click.style("System Default Ignore Patterns:", fg="cyan"))
    for pattern in system_patterns: click.echo(f"- {pattern}")
    
    click.echo(click.style("\nUser-Defined Ignore Patterns:", fg="cyan"))
    if user_patterns:
        for pattern in user_patterns: click.echo(f"- {pattern}")
    else:
        click.echo("  (None defined by user)")

@ignore_group.command("add")
@click.argument("pattern", type=str)
@click.pass_context
def ignore_add_command(ctx: click.Context, pattern: str):
    """Adds a new glob pattern to the user's ignore list."""
    global_args: GlobalArgs = ctx.obj
    try:
        if add_user_ignore_pattern(pattern, global_args.config_path_override):
            click.secho(f"Pattern '{pattern}' added.", fg="green")
        else:
            click.secho(f"Pattern '{pattern}' already exists.", fg="yellow")
    except CUEProviderError as e:
        click.secho(f"Error: {e}", fg="red", err=True)

@ignore_group.command("remove")
@click.argument("pattern", type=str)
@click.pass_context
def ignore_remove_command(ctx: click.Context, pattern: str):
    """Removes a glob pattern from the user's ignore list."""
    global_args: GlobalArgs = ctx.obj
    try:
        if remove_user_ignore_pattern(pattern, global_args.config_path_override):
            click.secho(f"Pattern '{pattern}' removed.", fg="green")
        else:
            click.secho(f"Pattern '{pattern}' not found.", fg="yellow")
    except CUEProviderError as e:
        click.secho(f"Error: {e}", fg="red", err=True)

@ignore_group.command("reset")
@click.confirmation_option(prompt="Are you sure you want to clear all user-defined ignore patterns?")
@click.pass_context
def ignore_reset_command(ctx: click.Context):
    """Clears all patterns from the user's ignore list."""
    global_args: GlobalArgs = ctx.obj
    try:
        reset_user_ignore_patterns(global_args.config_path_override)
        click.secho("User-defined ignore patterns cleared.", fg="green")
    except CUEProviderError as e:
        click.secho(f"Error: {e}", fg="red", err=True)

@cli_app.command("logs")
@click.option("-n", "--lines", type=int, help="Show the last N lines of the log file.")
@click.option("--raw", is_flag=True, help="Display raw log content without special formatting.")
@click.pass_context
def logs_command(ctx: click.Context, lines: Optional[int], raw: bool): 
    """Views application logs directly in the terminal."""
    global_args: GlobalArgs = ctx.obj
    log_file_to_view = global_args.log_file_override or get_log_file_path()
    
    if not log_file_to_view.exists():
        click.secho(f"Log file not found at: {log_file_to_view}", fg="red", err=True)
        return
    click.echo(f"Displaying logs from: {log_file_to_view}")
    asyncio.run(view_log_file(log_file_to_view, lines, False, raw))

if __name__ == '__main__':
    cli_app()
