import asyncio
import logging
from pathlib import Path
from typing import Optional, List
from uuid import uuid4
import sys

from .models import AppConfig, GlobalArgs, FailedUploadEntry
from .exceptions import UploadError, FileProcessingError, APIRequestError, ConfigError, IgnoredFileError
from .api_client import ApiClient
from .single_file_uploader import handle_single_file_upload
from .multipart_uploader import handle_multipart_upload
from .folder_processor import process_folder_upload
from .utils import get_file_size, validate_file_type
from .ignored_files_handler import is_path_ignored
from .failed_uploads_handler import FailedUploadsManager
from .logger_setup import rich_console


logger = logging.getLogger(__name__)


async def retry_failed_entries(
    entries: List[FailedUploadEntry],
    config: AppConfig,
    global_args: GlobalArgs,
    failed_log_path: Path,
    retry_rounds: int = 1
) -> tuple[int, int]:
    """
    Retries a list of FailedUploadEntry items.
    Returns tuple (succeeded_count, failed_count).
    """
    if not entries:
        return 0, 0

    rich_console.print(f"\n[bold cyan]Starting retry for {len(entries)} failed upload(s)...[/bold cyan]")
    succeeded_count = 0
    
    for round_idx in range(retry_rounds):
        current_failed = await FailedUploadsManager.get_failed_entries(failed_log_path)
        entry_ids_to_retry = {e.entry_id for e in entries if e.entry_id}
        entries_to_attempt = [e for e in current_failed if e.entry_id in entry_ids_to_retry]

        if not entries_to_attempt:
            break

        if round_idx > 0:
            rich_console.print(f"\n[bold yellow]--- Retry Round {round_idx + 1}/{retry_rounds} ---[/bold yellow]")

        for entry in entries_to_attempt:
            if not entry.file_path.exists():
                rich_console.print(f"[yellow]Warning: Local file no longer exists at path: {entry.file_path}. Skipping.[/yellow]")
                continue

            target_env = entry.env or global_args.env_cli or config.default_env
            file_concurrency = entry.file_concurrency or config.file_concurrency
            part_concurrency = entry.part_concurrency or config.part_concurrency

            from .auth_handler import get_api_key
            api_key, _ = get_api_key(global_args.api_key_cli, config, global_args)
            if not api_key:
                rich_console.print(f"[bold red]API Key not found for environment '{target_env}'. Cannot retry {entry.file_path.name}.[/bold red]")
                continue

            retry_global_args = GlobalArgs(
                api_key_cli=global_args.api_key_cli,
                env_cli=target_env,
                config_path_override=global_args.config_path_override,
                log_file_override=global_args.log_file_override,
                verbose_level=global_args.verbose_level,
                quiet_mode=global_args.quiet_mode,
                config=config,
            )

            api_client = ApiClient(config=config, global_args=retry_global_args, api_key=api_key)
            file_size = get_file_size(entry.file_path)
            multipart_threshold_bytes = config.multipart_threshold_gb * (1024**3)

            try:
                if file_size > multipart_threshold_bytes:
                    await handle_multipart_upload(
                        file_path=entry.file_path,
                        file_size=file_size,
                        collection=entry.collection,
                        target_sub_path=entry.target_sub_path,
                        api_client=api_client,
                        config=config,
                        part_concurrency=part_concurrency,
                        cached_checksum=entry.checksum,
                        cached_content_type=entry.content_type,
                    )
                else:
                    await handle_single_file_upload(
                        file_path=entry.file_path,
                        file_size=file_size,
                        collection=entry.collection,
                        target_sub_path=entry.target_sub_path,
                        api_client=api_client,
                        config=config,
                        cached_checksum=entry.checksum,
                        cached_content_type=entry.content_type,
                    )

                if entry.entry_id:
                    await FailedUploadsManager.remove_failed_entry(entry.entry_id, failed_log_path)
                succeeded_count += 1
                rich_console.print(f"[green]✓ Retried and successfully uploaded: {entry.file_path.name}[/green]")

            except Exception as e:
                logger.error(f"Retry failed for {entry.file_path.name}: {e}")
                rich_console.print(f"[red]✗ Retry failed for {entry.file_path.name}: {e}[/red]")
            finally:
                await api_client.close()

    remaining_entries = await FailedUploadsManager.get_failed_entries(failed_log_path)
    remaining_ids = {e.entry_id for e in entries if e.entry_id}
    failed_count = len([e for e in remaining_entries if e.entry_id in remaining_ids])
    return succeeded_count, failed_count


async def process_upload(
    source_path: Path,
    collection: str,
    target_sub_path: Optional[str],
    api_key: str,
    config: AppConfig,
    global_args: GlobalArgs,
    file_concurrency: int,
    part_concurrency: int,
    auto_approve: bool
):
    """
    Main entry point to process an upload request for a file or folder.
    """
    session_id = str(uuid4())
    selected_env_name = global_args.env_cli or config.default_env
    failed_log_path = config.failed_log_path

    try:
        if selected_env_name not in config.environments.model_fields:
            raise ConfigError(f"Environment '{selected_env_name}' not defined in config.")
    except AttributeError: 
        raise ConfigError(f"Environment URL for '{selected_env_name}' not found.")

    api_client = ApiClient(config=config, global_args=global_args, api_key=api_key)
    
    if source_path.is_file():
        if is_path_ignored(source_path, base_path=source_path.parent, config_path_override=global_args.config_path_override):
            raise IgnoredFileError(f"File '{source_path.name}' matches an active ignore pattern and will not be uploaded.")

    upload_error_occurred = False
    try:
        if not source_path.exists():
            raise FileProcessingError(f"Source path does not exist: {source_path}")
        
        if source_path.is_file():
            await validate_file_type(source_path, config)

        if source_path.is_dir():
            logger.info(f"Processing folder upload for: {source_path}")
            await process_folder_upload(
                folder_path=source_path, collection=collection, 
                target_sub_path=target_sub_path,
                api_client=api_client, config=config, global_args=global_args,
                file_concurrency=file_concurrency, part_concurrency=part_concurrency,
                auto_approve=auto_approve,
                session_id=session_id
            )
        elif source_path.is_file():
            logger.info(f"Processing file upload for: {source_path.name}")
            file_size = get_file_size(source_path)
            multipart_threshold_bytes = config.multipart_threshold_gb * (1024**3)

            if file_size == 0:
                rich_console.print(f"[yellow]Warning: Skipping file [bold]{source_path.name}[/bold] because it is 0 B.[/yellow]")
                return
            
            checksum: Optional[str] = None
            content_type: Optional[str] = None
            try:
                if file_size > multipart_threshold_bytes:
                    checksum, content_type = await handle_multipart_upload(
                        file_path=source_path, file_size=file_size, collection=collection,
                        target_sub_path=target_sub_path, api_client=api_client, config=config,
                        part_concurrency=part_concurrency
                    )
                else:
                    checksum, content_type = await handle_single_file_upload( 
                        file_path=source_path, file_size=file_size, collection=collection,
                        target_sub_path=target_sub_path, api_client=api_client, config=config
                    )
            except Exception as e:
                upload_error_occurred = True
                entry = FailedUploadEntry(
                    session_id=session_id,
                    file_path=source_path,
                    collection=collection,
                    target_sub_path=target_sub_path,
                    env=selected_env_name,
                    file_size=file_size,
                    content_type=content_type,
                    checksum=checksum,
                    file_concurrency=file_concurrency,
                    part_concurrency=part_concurrency,
                    error_message=str(e),
                )
                await FailedUploadsManager.add_failed_entry(entry, failed_log_path)
                raise
        else:
            raise FileProcessingError(f"Source path is not a file or directory: {source_path}")

    except (UploadError, FileProcessingError, APIRequestError, ConfigError, IgnoredFileError) as e:
        logger.error(f"Upload processing failed: {e}", exc_info=global_args.verbose_level >=2)
        upload_error_occurred = True
    except Exception as e:
        logger.critical(f"An unexpected critical error occurred in upload processing: {e}", exc_info=True)
        upload_error_occurred = True
    finally:
        if 'api_client' in locals() and api_client: 
            await api_client.close()
        logger.debug("process_upload primary pass finished.")

    # --- End of Session Handling ---
    session_failed_entries = await FailedUploadsManager.get_failed_entries(failed_log_path, session_id=session_id)
    if session_failed_entries:
        if config.auto_retry:
            rich_console.print(f"\n[bold yellow]Auto-retry is enabled. Attempting end-of-session retry for {len(session_failed_entries)} failed file(s)...[/bold yellow]")
            succeeded, failed = await retry_failed_entries(
                session_failed_entries, config, global_args, failed_log_path, retry_rounds=max(1, config.retry_attempts)
            )
            if failed > 0:
                rich_console.print(f"\n[bold red]Automatic retry completed: {failed} file(s) still failed. Details saved in: {failed_log_path}[/bold red]")
                raise UploadError(f"{failed} file(s) failed to upload after auto-retry.")
        else:
            rich_console.print(f"\n[bold yellow]Upload completed with {len(session_failed_entries)} failed file(s).[/bold yellow]")
            rich_console.print(f"Failed uploads saved to: [cyan]{failed_log_path}[/cyan]")
            rich_console.print("[dim]Tip: Run 'cue-upload retry' to attempt uploading failed files.[/dim]")
            raise UploadError(f"{len(session_failed_entries)} file(s) failed to upload during this session.")

