# cue_provider_utility/folder_processor.py

"""
Handles the processing of folder uploads.
Scans directories, filters files, manages concurrent uploads of individual files.
"""
import asyncio
import logging
from pathlib import Path
from typing import Any, Optional, List, Tuple
import click

from .models import AppConfig, GlobalArgs
from .exceptions import UploadError, FileProcessingError, APIRequestError, UploadCancelledError
from .api_client import ApiClient
from .utils import get_file_size, validate_file_type, format_bytes
from .ignored_files_handler import is_path_ignored
from .logger_setup import rich_console
from rich.progress import Progress, BarColumn, TextColumn, TaskProgressColumn, TimeElapsedColumn, TaskID

logger = logging.getLogger(__name__)

class FileToUpload:
    """Represents a file discovered during folder scan, ready for upload."""
    def __init__(self, local_path: Path, relative_path: Path, size: int):
        self.local_path = local_path
        self.relative_path = relative_path
        self.size = size
        self.status: str = "pending"
        self.error_message: Optional[str] = None
        self.task_id: Optional[TaskID] = None # Will hold the ID for its own progress bar task

    def __repr__(self):
        return f"<FileToUpload {self.relative_path} status={self.status}>"


async def scan_folder(
    folder_path: Path,
    config: AppConfig,
    config_path_override: Optional[Path]
) -> Tuple[List[FileToUpload], int, int]:
    """
    Scans a folder recursively, applying ignore patterns and file type validation.
    Returns a list of FileToUpload objects, total file count, and total size.
    """
    files_to_upload: List[FileToUpload] = []
    total_files_for_upload = 0
    total_size_for_upload = 0

    rich_console.print(f"[info]Scanning folder: [cyan]{folder_path}[/cyan]...")
    
    for item in folder_path.rglob("*"):
        try:
            if is_path_ignored(item, base_path=folder_path, config_path_override=config_path_override):
                logger.debug(f"Ignoring path due to ignore rules: {item.relative_to(folder_path)}")
                continue

            if item.is_file():
                try:
                    await validate_file_type(item, config)
                except FileProcessingError as e:
                    logger.warning(str(e))
                    rich_console.print(f"[yellow]Warning: Skipped file [bold]{item.name}[/bold]. Reason: {e}[/yellow]")
                    continue

                file_size = get_file_size(item)
                relative_path = item.relative_to(folder_path)
                files_to_upload.append(FileToUpload(item, relative_path, file_size))
                total_files_for_upload += 1
                total_size_for_upload += file_size
        
        except Exception as e:
            logger.error(f"Error processing path {item} during scan: {e}")
            rich_console.print(f"[red]Error scanning item {item.relative_to(folder_path)}: {e}[/red]")

    logger.info(f"Folder scan complete. Found {total_files_for_upload} files for upload ({format_bytes(total_size_for_upload)}).")
    return files_to_upload, total_files_for_upload, total_size_for_upload


async def _upload_one_file_from_folder(
    file_task: FileToUpload,
    collection: str,
    base_target_sub_path: Optional[str],
    api_client: ApiClient,
    config: AppConfig,
    part_concurrency: int,
    progress: Progress,
    # This ID is for the main folder task, to be advanced after each file.
    overall_folder_task_id: Any
):
    """Handles uploading a single file as part of a folder upload."""
    # Import functions here to avoid circular dependencies
    from .single_file_uploader import handle_single_file_upload
    from .multipart_uploader import handle_multipart_upload

    file_task.status = "uploading"
    
    # Each file gets its own progress bar, nested under the main folder task.
    file_task.task_id = progress.add_task(f"[dim]{file_task.relative_path}[/dim]", total=file_task.size, start=False, parent=overall_folder_task_id)

    effective_api_target_sub_path = base_target_sub_path if base_target_sub_path else ""
    if file_task.relative_path.parent != Path("."):
        effective_api_target_sub_path = str(Path(effective_api_target_sub_path) / file_task.relative_path.parent)
    
    try:
        progress.start_task(file_task.task_id)

        multipart_threshold_bytes = config.multipart_threshold_gb * (1024**3)
        if file_task.size > multipart_threshold_bytes:
            await handle_multipart_upload(
                file_path=file_task.local_path, file_size=file_task.size,
                collection=collection, target_sub_path=effective_api_target_sub_path,
                api_client=api_client, config=config,
                part_concurrency=part_concurrency,
                progress=progress,
                #  Pass the specific ID for this file's task
                file_task_id=file_task.task_id
            )
        else:
            await handle_single_file_upload(
                file_path=file_task.local_path, file_size=file_task.size,
                collection=collection, target_sub_path=effective_api_target_sub_path,
                api_client=api_client, config=config,
                progress=progress,
                # Pass the specific ID for this file's task
                task_id=file_task.task_id
            )
        file_task.status = "success"
        progress.update(file_task.task_id, description=f"[green]✓[/green] {file_task.relative_path}", completed=file_task.size)

    except (UploadError, FileProcessingError, APIRequestError) as e:
        logger.error(f"Failed to upload file {file_task.relative_path}: {e}")
        file_task.status = "failed"
        file_task.error_message = str(e)
        progress.update(file_task.task_id, description=f"[red]✗[/red] {file_task.relative_path} ([i]{e}[/i])")
    except Exception as e:
        logger.critical(f"Unexpected critical error uploading file {file_task.relative_path}: {e}", exc_info=True)
        file_task.status = "failed"
        file_task.error_message = f"An unexpected critical error occurred: {e}"
        progress.update(file_task.task_id, description=f"[bold red]CRITICAL ERROR[/bold red] {file_task.relative_path}")
    finally:
        # Advance the main folder task by one file count.
        progress.update(overall_folder_task_id, advance=1)


async def process_folder_upload(
    folder_path: Path,
    collection: str,
    target_sub_path: Optional[str],
    api_client: ApiClient,
    config: AppConfig,
    global_args: GlobalArgs,
    file_concurrency: int,
    part_concurrency: int,
    auto_approve: bool
):
    """Orchestrates the upload of all files within a folder."""
    
    files_to_upload_list, num_files, total_size = await scan_folder(folder_path, config, global_args.config_path_override)

    if not files_to_upload_list:
        rich_console.print("[yellow]No files found to upload in the specified folder (after applying ignore rules and file type validation).[/yellow]")
        return

    rich_console.print(f"Found [bold]{num_files}[/bold] files to upload, total size: [bold]{format_bytes(total_size)}[/bold].")
    
    if not auto_approve:
        rich_console.print("Files to be uploaded (first 10 shown):")
        for f_task_item in files_to_upload_list[:10]:
            rich_console.print(f"  - {f_task_item.relative_path} ({format_bytes(f_task_item.size)})")
        if num_files > 10:
            rich_console.print(f"  ...and {num_files - 10} more file(s).")
        
        if not click.confirm("\nProceed with upload?", default=True):
            raise UploadCancelledError("Upload cancelled by user.")

    semaphore = asyncio.Semaphore(file_concurrency)
    
    # This is the single, top-level Progress bar manager
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=rich_console
    ) as progress:
        # The main task now tracks the NUMBER of files.
        folder_task_id = progress.add_task(f"[bold]Uploading folder: {folder_path.name}[/bold]", total=num_files)

        async def worker(file_to_upload_item: FileToUpload):
            async with semaphore:
                await _upload_one_file_from_folder(
                    file_task=file_to_upload_item, collection=collection,
                    base_target_sub_path=target_sub_path, api_client=api_client, config=config,
                    part_concurrency=part_concurrency,
                    progress=progress, 
                    overall_folder_task_id=folder_task_id
                )

        await asyncio.gather(*(worker(f_task) for f_task in files_to_upload_list))

    # Final summary
    successful_uploads = [f for f in files_to_upload_list if f.status == "success"]
    failed_uploads = [f for f in files_to_upload_list if f.status == "failed"]
    
    rich_console.print("\n--- Folder Upload Summary ---")
    rich_console.print(f"[green]Successfully uploaded: {len(successful_uploads)} files[/green]")
    if failed_uploads:
        rich_console.print(f"[bold red]Failed to upload: {len(failed_uploads)} files[/bold red]")
        rich_console.print("Failed file details:")
        for f_task in failed_uploads:
            error_msg = (f_task.error_message or 'Unknown error').splitlines()[0]
            rich_console.print(f"  - [red]{f_task.relative_path}[/red]: {error_msg}")
        raise UploadError(f"{len(failed_uploads)} files failed to upload. Check logs for details.")