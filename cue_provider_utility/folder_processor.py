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
from .exceptions import UploadError, FileProcessingError, APIRequestError
from .api_client import ApiClient
from .utils import get_file_size, is_file_type_disallowed, format_bytes
from .ignored_files_handler import is_path_ignored
from .logger_setup import rich_console
from rich.progress import Progress, BarColumn, TextColumn, TaskProgressColumn, TimeElapsedColumn

logger = logging.getLogger(__name__)

class FileToUpload:
    """Represents a file discovered during folder scan, ready for upload."""
    def __init__(self, local_path: Path, relative_path: Path, size: int):
        self.local_path = local_path
        self.relative_path = relative_path 
        self.size = size
        self.status: str = "pending" 
        self.error_message: Optional[str] = None

    def __repr__(self):
        return f"<FileToUpload {self.relative_path} status={self.status}>"


async def scan_folder(folder_path: Path, config_path_override: Optional[Path]) -> Tuple[List[FileToUpload], int, int]:
    """
    Scans a folder recursively, applying ignore patterns.
    Returns a list of FileToUpload objects, total file count, and total size.
    """
    files_to_upload: List[FileToUpload] = []
    total_items_scanned = 0 # Includes files and dirs initially encountered by rglob
    total_files_for_upload = 0
    total_size_for_upload = 0
    ignored_items_count = 0

    rich_console.print(f"[info]Scanning folder: [cyan]{folder_path}[/cyan]...")
    
    for item in folder_path.rglob("*"): 
        total_items_scanned +=1
        try:
            # Check if the item itself or any of its parents match an ignore pattern
            # This is important for ignoring entire directories like .git/ or node_modules/
            # by checking each part of the relative path if the pattern contains '/'
            
            # More robust ignore check: check item itself, then check if any parent is ignored if it's a directory pattern
            # The current is_path_ignored might need refinement for directory patterns like "node_modules/"
            # to correctly skip all children if "node_modules/" itself is matched.
            # For now, assume is_path_ignored handles this.
            if is_path_ignored(item, base_path=folder_path, config_path_override=config_path_override):
                logger.debug(f"Ignoring path due to ignore rules: {item.relative_to(folder_path)}")
                ignored_items_count +=1
                # If 'item' is a directory and it's ignored, rglob will still yield its children.
                # A more advanced scan might skip descending into ignored directories.
                # For now, `is_path_ignored` will be called for children too.
                continue

            if item.is_file():
                if is_file_type_disallowed(item): 
                    logger.warning(f"Skipping disallowed file type: {item.name} (extension: {item.suffix})")
                    rich_console.print(f"[yellow]Warning: Skipped disallowed file type [bold]{item.name}[/bold].[/yellow]")
                    continue

                file_size = get_file_size(item)
                relative_path = item.relative_to(folder_path)
                files_to_upload.append(FileToUpload(item, relative_path, file_size))
                total_files_for_upload +=1
                total_size_for_upload += file_size
            # Directories themselves are not added to files_to_upload list, only their file contents.
            # `is_path_ignored` should handle ignoring directories like `.git/`

        except Exception as e:
            logger.error(f"Error processing path {item} during scan: {e}")
            rich_console.print(f"[red]Error scanning item {item.relative_to(folder_path)}: {e}[/red]")

    logger.info(f"Folder scan complete. Found {total_files_for_upload} files to upload ({format_bytes(total_size_for_upload)}). "
                f"Total items iterated: {total_items_scanned}, Ignored items/paths: {ignored_items_count}.")
    return files_to_upload, total_files_for_upload, total_size_for_upload


async def _upload_one_file_from_folder(
    file_task: FileToUpload,
    collection: str,
    base_target_sub_path: Optional[str], 
    api_client: ApiClient,
    config: AppConfig,
    global_args: GlobalArgs,
    part_concurrency: int,
    progress: Progress, 
    overall_folder_task_id: Any # Rich TaskID for the main folder progress
):
    """Handles uploading a single file as part of a folder upload."""
    from .single_file_uploader import handle_single_file_upload
    from .multipart_uploader import handle_multipart_upload

    file_task.status = "uploading"
    
    # Construct the target_sub_path for this specific file for the API call
    # This is the prefix under which the file (using its original name) will be placed.
    # If base_target_sub_path = "user_prefix" and file_task.relative_path = "subdir/file.txt",
    # the effective_target_sub_path for the API call (for single/multi) becomes "user_prefix/subdir".
    # The filename "file.txt" is implicitly handled by the single/multi uploader using file_task.local_path.name.
    effective_api_target_sub_path = base_target_sub_path if base_target_sub_path else ""
    if file_task.relative_path.parent != Path("."): 
        effective_api_target_sub_path = str(Path(effective_api_target_sub_path) / file_task.relative_path.parent)
    
    # Individual file progress (can be noisy, consider enabling with verbosity)
    # For now, rely on progress messages from single/multi uploader, and update overall folder task.
    # file_specific_progress_desc = f"  {file_task.relative_path} ({format_bytes(file_task.size)})"
    # file_specific_task_id = progress.add_task(file_specific_progress_desc, total=file_task.size, visible=False) # Initially not visible or start=False

    try:
        # progress.update(file_specific_task_id, visible=True, description=f"[cyan]Starting: {file_task.relative_path}[/cyan]")
        # progress.start_task(file_specific_task_id)
        rich_console.print(f"Starting upload: {file_task.relative_path} ({format_bytes(file_task.size)})")


        multipart_threshold_bytes = config.multipart_threshold_gb * (1024**3)
        if file_task.size > multipart_threshold_bytes:
            await handle_multipart_upload(
                file_path=file_task.local_path,
                file_size=file_task.size,
                collection=collection,
                target_sub_path=effective_api_target_sub_path, 
                api_client=api_client,
                config=config,
                global_args=global_args,
                part_concurrency=part_concurrency
            )
        else:
            await handle_single_file_upload(
                file_path=file_task.local_path,
                file_size=file_task.size,
                collection=collection,
                target_sub_path=effective_api_target_sub_path, 
                api_client=api_client,
                config=config,
                global_args=global_args
            )
        file_task.status = "success"

        progress.update(overall_folder_task_id, advance=1) 

    except (UploadError, FileProcessingError, APIRequestError) as e:
        logger.error(f"Failed to upload file {file_task.local_path} (relative: {file_task.relative_path}): {e}")
        file_task.status = "failed"
        file_task.error_message = str(e)
        progress.update(overall_folder_task_id, advance=1) # Still advance, as file processing is "done" (failed)
        rich_console.print(f"[red]Failed: {file_task.relative_path} - {str(e).splitlines()[0]}[/red]")


    except Exception as e:
        logger.critical(f"Unexpected critical error uploading file {file_task.local_path} (relative: {file_task.relative_path}): {e}", exc_info=True)
        file_task.status = "failed"
        file_task.error_message = f"Unexpected critical error: {e}"
        progress.update(overall_folder_task_id, advance=1)
        rich_console.print(f"[bold red]CRITICAL ERROR during upload of {file_task.relative_path}: {e}[/bold red]")


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
    
    files_to_upload_list, num_files_for_upload, total_size_for_upload = await scan_folder(folder_path, global_args.config_path_override)

    if not files_to_upload_list:
        rich_console.print("[yellow]No files found to upload in the specified folder (after applying ignore rules).[/yellow]")
        return

    rich_console.print(f"Found [bold]{num_files_for_upload}[/bold] files to upload, total size: [bold]{format_bytes(total_size_for_upload)}[/bold].")
    
    if num_files_for_upload > 0 and not auto_approve:
        rich_console.print("Files to upload (first 5 shown):")
        for f_task_item in files_to_upload_list[:5]:
            rich_console.print(f"  - {f_task_item.relative_path} ({format_bytes(f_task_item.size)})")
        if num_files_for_upload > 5:
            rich_console.print(f"  ...and {num_files_for_upload - 5} more files.")
        
        if not click.confirm("Proceed with upload?", default=True):
            rich_console.print("[yellow]Upload cancelled by user.[/yellow]")
            return

    semaphore = asyncio.Semaphore(file_concurrency)
    async_upload_tasks: List[asyncio.Task] = [] 
    
    successful_uploads_count = 0 
    failed_uploads_count = 0 

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(), 
        TimeElapsedColumn(),
        console=rich_console
    ) as overall_progress:
        folder_progress_task_id = overall_progress.add_task(f"Uploading folder: {folder_path.name}", total=num_files_for_upload) 

        async def folder_file_worker_wrapper(file_to_upload_item: FileToUpload): 
            nonlocal successful_uploads_count, failed_uploads_count 
            async with semaphore:
                await _upload_one_file_from_folder(
                    file_task=file_to_upload_item,
                    collection=collection,
                    base_target_sub_path=target_sub_path,
                    api_client=api_client,
                    config=config,
                    global_args=global_args,
                    part_concurrency=part_concurrency,
                    progress=overall_progress, 
                    overall_folder_task_id=folder_progress_task_id
                )
            # Status is updated within _upload_one_file_from_folder
            if file_to_upload_item.status == "success":
                successful_uploads_count +=1
            else: # 'failed' or other non-success states
                failed_uploads_count +=1
            # Overall progress is advanced within _upload_one_file_from_folder after each file attempt

        for f_task_item_loop in files_to_upload_list:
            async_task_item = asyncio.create_task(folder_file_worker_wrapper(f_task_item_loop))
            async_upload_tasks.append(async_task_item)
            
        if async_upload_tasks: # Ensure there are tasks to gather
            await asyncio.gather(*async_upload_tasks) 

    rich_console.print("\n--- Folder Upload Summary ---")
    rich_console.print(f"Total files processed: {num_files_for_upload}")
    rich_console.print(f"[green]Successfully uploaded: {successful_uploads_count} files[/green]")
    if failed_uploads_count > 0:
        rich_console.print(f"[bold red]Failed to upload: {failed_uploads_count} files[/bold red]")
        rich_console.print("Failed file details:")
        for f_task_summary in files_to_upload_list:
            if f_task_summary.status == "failed":
                error_msg_short = (f_task_summary.error_message or 'Unknown error').splitlines()[0]
                rich_console.print(f"  - [red]{f_task_summary.relative_path}[/red]: {error_msg_short}")
    elif successful_uploads_count == num_files_for_upload and num_files_for_upload > 0 :
        rich_console.print("All files uploaded successfully.")
    elif num_files_for_upload == 0 : # Should have been caught earlier
        pass # Already handled by "No files found"
    
    if failed_uploads_count > 0:
        raise UploadError(f"{failed_uploads_count} files failed to upload during folder processing. Check logs for details.")
