import logging

import click
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .models import AppConfig, GlobalArgs, FailedUploadEntry
from .exceptions import (
    UploadError,
    FileProcessingError,
    APIRequestError,
    ConfigError,
    IgnoredFileError,
    UploadCancelledError,
)
from .api_client import ApiClient
from .single_file_uploader import handle_single_file_upload
from .multipart_uploader import handle_multipart_upload
from .folder_processor import (
    FileToUpload,
    ScanCounts,
    iter_scan_folder,
    process_batch,
    process_folder_upload,
    remote_path,
    report_file_failure,
    scan_event,
)
from .utils import get_file_size, validate_file_type
from .ignored_files_handler import is_path_ignored
from .failed_uploads_handler import FailedUploadsManager
from .logger_setup import rich_console
from .progress_adapter import JsonProgress
from . import events, messages, resume_log


logger = logging.getLogger(__name__)


def _event_progress(file_id: str, size: int) -> Tuple[Any, Any]:
    """In events mode returns a bound ``JsonProgress`` task, otherwise ``(None, None)``."""
    if not events.enabled():
        return None, None
    progress = JsonProgress()
    task_id = progress.add_task("upload", total=size)
    progress.bind(task_id, file_id)
    return progress, task_id


async def retry_failed_entries(
    entries: List[FailedUploadEntry],
    config: AppConfig,
    global_args: GlobalArgs,
    failed_log_path: Path,
    retry_rounds: int = 1,
) -> tuple[int, int]:
    """
    Retries a list of FailedUploadEntry items.
    Returns tuple (succeeded_count, failed_count).
    """
    if not entries:
        return 0, 0

    rich_console.print(
        f"\n[bold cyan]Starting retry for {len(entries)} failed upload(s)...[/bold cyan]"
    )
    succeeded_count = 0
    session = events.current()

    for round_idx in range(retry_rounds):
        current_failed = await FailedUploadsManager.get_failed_entries(failed_log_path)
        entry_ids_to_retry = {e.entry_id for e in entries if e.entry_id}
        entries_to_attempt = [
            e for e in current_failed if e.entry_id in entry_ids_to_retry
        ]

        if not entries_to_attempt:
            break

        last_round = round_idx == retry_rounds - 1
        events.emit(
            "retry_round",
            round=round_idx + 1,
            max_rounds=retry_rounds,
            files=len(entries_to_attempt),
        )
        if round_idx > 0:
            rich_console.print(
                f"\n[bold yellow]--- Retry Round {round_idx + 1}/{retry_rounds} ---[/bold yellow]"
            )

        for entry in entries_to_attempt:
            file_id = session.file_id_for(entry.file_path, entry.file_size)
            if not entry.file_path.exists():
                rich_console.print(
                    f"[yellow]Warning: Local file no longer exists at path: {entry.file_path}. Skipping.[/yellow]"
                )
                if last_round:
                    session.stage(file_id, "FAILED")
                    messages.report(
                        "FILE_READ",
                        file_id=file_id,
                        failed_entry_id=entry.entry_id,
                        detail=f"Local file no longer exists: {entry.file_path}",
                    )
                continue

            target_env = entry.env or global_args.env_cli or config.default_env
            part_concurrency = entry.part_concurrency or config.part_concurrency

            from .auth_handler import get_api_key

            api_key, _ = get_api_key(global_args.api_key_cli, config, global_args)
            if not api_key:
                rich_console.print(
                    f"[bold red]API Key not found for environment '{target_env}'. Cannot retry {entry.file_path.name}.[/bold red]"
                )
                if last_round:
                    session.stage(file_id, "FAILED")
                    messages.report(
                        "AUTH_MISSING",
                        file_id=file_id,
                        failed_entry_id=entry.entry_id,
                        env=target_env,
                    )
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

            api_client = ApiClient(
                config=config, global_args=retry_global_args, api_key=api_key
            )
            file_size = get_file_size(entry.file_path)
            multipart_threshold_bytes = config.multipart_threshold_gb * (1024**3)
            progress, task_id = _event_progress(file_id, file_size)
            session.stage(
                file_id, "RETRYING", attempt=round_idx + 1, max_attempts=retry_rounds
            )

            try:
                if file_size > multipart_threshold_bytes:
                    result = await handle_multipart_upload(
                        file_path=entry.file_path,
                        file_size=file_size,
                        collection=entry.collection,
                        target_sub_path=entry.target_sub_path,
                        api_client=api_client,
                        config=config,
                        part_concurrency=part_concurrency,
                        progress=progress,
                        file_task_id=task_id,
                        cached_checksum=entry.checksum,
                        cached_content_type=entry.content_type,
                    )
                else:
                    result = await handle_single_file_upload(
                        file_path=entry.file_path,
                        file_size=file_size,
                        collection=entry.collection,
                        target_sub_path=entry.target_sub_path,
                        api_client=api_client,
                        config=config,
                        progress=progress,
                        task_id=task_id,
                        cached_checksum=entry.checksum,
                        cached_content_type=entry.content_type,
                    )

                if entry.entry_id:
                    await FailedUploadsManager.remove_failed_entry(
                        entry.entry_id, failed_log_path
                    )
                succeeded_count += 1
                session.file_done(
                    file_id,
                    result[0],
                    result[1],
                    remote_path(
                        entry.collection, entry.target_sub_path, entry.file_path.name
                    ),
                )
                messages.report("RETRY_OK", file_id=file_id, name=entry.file_path.name)
                rich_console.print(
                    f"[green]✓ Retried and successfully uploaded: {entry.file_path.name}[/green]"
                )

            except Exception as e:
                logger.error(f"Retry failed for {entry.file_path.name}: {e}")
                rich_console.print(
                    f"[red]✗ Retry failed for {entry.file_path.name}: {e}[/red]"
                )
                if last_round:
                    session.stage(file_id, "FAILED")
                    messages.report_exception(
                        e,
                        file_id=file_id,
                        failed_entry_id=entry.entry_id,
                        name=entry.file_path.name,
                    )
                else:
                    session.stage(file_id, "RETRY_PENDING")
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
    auto_approve: bool,
) -> None:
    """
    Main entry point to process an upload request for a file or folder.
    """
    session_id = events.current().session_id
    selected_env_name = global_args.env_cli or config.default_env
    failed_log_path = config.failed_log_path

    try:
        if selected_env_name not in config.environments.model_fields:
            raise ConfigError(
                f"Environment '{selected_env_name}' not defined in config."
            )
    except AttributeError:
        raise ConfigError(f"Environment URL for '{selected_env_name}' not found.")

    api_client = ApiClient(config=config, global_args=global_args, api_key=api_key)

    if source_path.is_file():
        if is_path_ignored(
            source_path,
            base_path=source_path.parent,
            config_path_override=global_args.config_path_override,
        ):
            raise IgnoredFileError(
                f"File '{source_path.name}' matches an active ignore pattern and will not be uploaded."
            )

    try:
        if not source_path.exists():
            raise FileProcessingError(f"Source path does not exist: {source_path}")

        if source_path.is_file():
            await validate_file_type(source_path, config)

        if source_path.is_dir():
            logger.info(f"Processing folder upload for: {source_path}")
            await process_folder_upload(
                folder_path=source_path,
                collection=collection,
                target_sub_path=target_sub_path,
                api_client=api_client,
                config=config,
                global_args=global_args,
                file_concurrency=file_concurrency,
                part_concurrency=part_concurrency,
                auto_approve=auto_approve,
                session_id=session_id,
            )
        elif source_path.is_file():
            logger.info(f"Processing file upload for: {source_path.name}")
            file_size = get_file_size(source_path)
            multipart_threshold_bytes = config.multipart_threshold_gb * (1024**3)

            if file_size == 0:
                rich_console.print(
                    f"[yellow]Warning: Skipping file [bold]{source_path.name}[/bold] because it is 0 B.[/yellow]"
                )
                messages.report("SKIPPED_EMPTY", name=source_path.name)
                return

            file_id = events.current().file_id_for(source_path, file_size)
            progress, task_id = _event_progress(file_id, file_size)
            events.emit(
                "batch_start",
                files=1,
                bytes=file_size,
                collection=collection,
                target_path=target_sub_path,
                file_concurrency=file_concurrency,
                part_concurrency=part_concurrency,
            )

            checksum: Optional[str] = None
            content_type: Optional[str] = None
            try:
                if file_size > multipart_threshold_bytes:
                    checksum, content_type = await handle_multipart_upload(
                        file_path=source_path,
                        file_size=file_size,
                        collection=collection,
                        target_sub_path=target_sub_path,
                        api_client=api_client,
                        config=config,
                        part_concurrency=part_concurrency,
                        progress=progress,
                        file_task_id=task_id,
                    )
                else:
                    checksum, content_type = await handle_single_file_upload(
                        file_path=source_path,
                        file_size=file_size,
                        collection=collection,
                        target_sub_path=target_sub_path,
                        api_client=api_client,
                        config=config,
                        progress=progress,
                        task_id=task_id,
                    )
                events.current().file_done(
                    file_id,
                    checksum,
                    content_type,
                    remote_path(collection, target_sub_path, source_path.name),
                )
                messages.report(
                    "UPLOAD_OK",
                    file_id=file_id,
                    name=source_path.name,
                    collection=collection,
                )
            except Exception as e:
                checksum_val = getattr(e, "checksum", checksum)
                content_type_val = getattr(e, "content_type", content_type)
                entry = FailedUploadEntry(
                    session_id=session_id,
                    file_path=source_path,
                    collection=collection,
                    target_sub_path=target_sub_path,
                    env=selected_env_name,
                    file_size=file_size,
                    content_type=content_type_val,
                    checksum=checksum_val,
                    file_concurrency=file_concurrency,
                    part_concurrency=part_concurrency,
                    error_message=str(e),
                )
                saved = await FailedUploadsManager.add_failed_entry(
                    entry, failed_log_path
                )
                report_file_failure(
                    file_id, e, config, saved.entry_id, source_path.name
                )
                raise
        else:
            raise FileProcessingError(
                f"Source path is not a file or directory: {source_path}"
            )

    except (UploadCancelledError, click.ClickException):
        # Declined confirmation / cancel / usage problems go back to the command.
        raise
    except (
        UploadError,
        FileProcessingError,
        APIRequestError,
        ConfigError,
        IgnoredFileError,
    ) as e:
        logger.error(
            f"Upload processing failed: {e}", exc_info=global_args.verbose_level >= 2
        )
    except Exception as e:
        logger.critical(
            f"An unexpected critical error occurred in upload processing: {e}",
            exc_info=True,
        )
    finally:
        if "api_client" in locals() and api_client:
            await api_client.close()
        logger.debug("process_upload primary pass finished.")

    await finish_session(session_id, config, global_args)


class UploadItem:
    """One ``-P`` path or manifest entry; ``target_path`` overrides ``-tp``."""

    def __init__(
        self, path: Path, target_path: Optional[str] = None, from_manifest: bool = False
    ) -> None:
        self.path = path
        self.target_path = (
            target_path.replace("\\", "/").strip("/") if target_path else target_path
        )
        self.from_manifest = from_manifest


async def _collect_items(
    items: List[UploadItem],
    target_sub_path: Optional[str],
    config: AppConfig,
    global_args: GlobalArgs,
) -> List[FileToUpload]:
    """Scans every item (folders stream; files are checked like folder entries)."""
    counts = ScanCounts()
    files: List[FileToUpload] = []
    override = global_args.config_path_override
    for item in items:
        target = item.target_path if item.target_path is not None else target_sub_path
        path = item.path
        if path.is_dir():
            async for f in iter_scan_folder(
                path, config, override, counts, target, item.from_manifest
            ):
                files.append(f)
            continue
        rel = Path(path.name)
        if not path.is_file():
            rich_console.print(
                f"[yellow]Warning: Skipped [bold]{path}[/bold]: not found.[/yellow]"
            )
            counts.rejected += 1
            scan_event(path, rel, 0, "rejected", "NOT_FOUND")
            continue
        if is_path_ignored(path, base_path=path.parent, config_path_override=override):
            rich_console.print(
                f"[yellow]Warning: Skipped file [bold]{path.name}[/bold]: matches an ignore pattern.[/yellow]"
            )
            counts.ignored += 1
            scan_event(path, rel, None, "ignored", "IGNORED")
            continue
        try:
            await validate_file_type(path, config)
        except FileProcessingError as e:
            rich_console.print(
                f"[yellow]Warning: Skipped file [bold]{path.name}[/bold]. Reason: {e}[/yellow]"
            )
            counts.rejected += 1
            scan_event(path, rel, None, "rejected", "FILE_TYPE_DENIED")
            continue
        size = get_file_size(path)
        if size == 0:
            rich_console.print(
                f"[yellow]Warning: Skipping file [bold]{path.name}[/bold] because it is 0 B.[/yellow]"
            )
            counts.empty += 1
            scan_event(path, rel, 0, "empty", "EMPTY")
            continue
        counts.files += 1
        counts.bytes += size
        scan_event(path, rel, size, "include")
        files.append(FileToUpload(path, rel, size, target or "", item.from_manifest))
    counts.emit_summary()
    return files


async def process_upload_items(
    items: List[UploadItem],
    collection: str,
    target_sub_path: Optional[str],
    api_key: str,
    config: AppConfig,
    global_args: GlobalArgs,
    file_concurrency: int,
    part_concurrency: int,
    auto_approve: bool,
) -> None:
    """Uploads several paths / manifest items in one session (one shared semaphore)."""
    session_id = events.current().session_id
    selected_env_name = global_args.env_cli or config.default_env
    if selected_env_name not in type(config.environments).model_fields:
        raise ConfigError(f"Environment '{selected_env_name}' not defined in config.")

    files = await _collect_items(items, target_sub_path, config, global_args)
    # "Start again" in the desktop app: skip what the earlier run already uploaded.
    files, already = resume_log.split_done(files, collection)
    if already:
        rich_console.print(
            f"[info]{already} file(s) already uploaded in the earlier run were skipped.[/info]"
        )
        messages.report("RESUME_SKIPPED", count=already)
    if not files:
        rich_console.print(
            "[yellow]No files found to upload (after applying ignore rules and file type validation).[/yellow]"
        )
        return

    api_client = ApiClient(config=config, global_args=global_args, api_key=api_key)
    try:
        await process_batch(
            files,
            label=f"Uploading {len(items)} item(s)",
            collection=collection,
            target_sub_path=target_sub_path,
            api_client=api_client,
            config=config,
            global_args=global_args,
            file_concurrency=file_concurrency,
            part_concurrency=part_concurrency,
            auto_approve=auto_approve,
            session_id=session_id,
        )
    except UploadError as e:
        # Per-file failures are already in the failed log; handled below.
        logger.error(f"Upload processing failed: {e}")
    finally:
        await api_client.close()

    await finish_session(session_id, config, global_args)


async def finish_session(
    session_id: str, config: AppConfig, global_args: GlobalArgs
) -> None:
    """End of session: auto-retry or point the user at ``cue-upload retry``."""
    failed_log_path = config.failed_log_path
    session_failed_entries = await FailedUploadsManager.get_failed_entries(
        failed_log_path, session_id=session_id
    )
    if session_failed_entries:
        if config.auto_retry:
            rich_console.print(
                f"\n[bold yellow]Auto-retry is enabled. Attempting end-of-session retry for {len(session_failed_entries)} failed file(s)...[/bold yellow]"
            )
            succeeded, failed = await retry_failed_entries(
                session_failed_entries,
                config,
                global_args,
                failed_log_path,
                retry_rounds=max(1, config.retry_attempts),
            )
            if failed > 0:
                rich_console.print(
                    f"\n[bold red]Automatic retry completed: {failed} file(s) still failed. Details saved in: {failed_log_path}[/bold red]"
                )
                raise UploadError(
                    f"{failed} file(s) failed to upload after auto-retry."
                )
        else:
            rich_console.print(
                f"\n[bold yellow]Upload completed with {len(session_failed_entries)} failed file(s).[/bold yellow]"
            )
            rich_console.print(
                f"Failed uploads saved to: [cyan]{failed_log_path}[/cyan]"
            )
            rich_console.print(
                "[dim]Tip: Run 'cue-upload retry' to attempt uploading failed files.[/dim]"
            )
            raise UploadError(
                f"{len(session_failed_entries)} file(s) failed to upload during this session."
            )
