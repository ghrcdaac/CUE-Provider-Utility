"""
Handles the multipart upload process for large files.
Coordinates with ApiClient to:
1. Initiate multipart upload.
2. Upload parts (concurrently).
3. Complete or abort the multipart upload.
"""
import asyncio
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any 
import math

from .models import (
    AppConfig, GlobalArgs, PartInfo,
    MultipartStartRequest, MultipartGetPartUrlRequest, MultipartCompleteRequest, MultipartAbortRequest
)
from .exceptions import UploadError, FileProcessingError, APIRequestError
from .api_client import ApiClient
from .utils import (
    calculate_sha256_checksum, calculate_sha256_checksum_for_bytes,
    get_mime_type, read_file_chunks, format_bytes
)
from .logger_setup import rich_console
from rich.progress import Progress, BarColumn, TextColumn, TransferSpeedColumn, TimeRemainingColumn, SpinnerColumn
import aiofiles # For UploadPartTask file reading

logger = logging.getLogger(__name__)

S3_MAX_PARTS = 10000

class UploadPartTask:
    """Helper class to manage state for uploading a single part."""
    def __init__(self, part_number: int, offset: int, size: int, file_path: Path):
        self.part_number = part_number
        self.offset = offset
        self.size = size
        self.file_path = file_path
        self.data: Optional[bytes] = None
        self.checksum_sha256: Optional[str] = None
        self.etag: Optional[str] = None
        self.error: Optional[Exception] = None
        self.retries: int = 0

    async def read_data(self):
        """Reads the part data from the file."""
        try:
            async with aiofiles.open(self.file_path, "rb") as f:
                await f.seek(self.offset)
                self.data = await f.read(self.size)
            if len(self.data) != self.size: 
                self.error = FileProcessingError(f"Read incorrect amount of data for part {self.part_number}. Expected {self.size}, got {len(self.data)}")
                raise self.error # Raise immediately
        except OSError as e:
            self.error = FileProcessingError(f"Error reading data for part {self.part_number}", original_exception=e)
            raise self.error # Raise immediately

    async def calculate_checksum(self):
        """Calculates checksum for the part data."""
        if self.data is None: # Data should have been read before this
             # Safety: attempt to read if not present, though ideally flow ensures it.
            logger.warning(f"Part {self.part_number} data was None before checksum calculation. Attempting read.")
            await self.read_data() 
        
        if self.data: # Check again as read_data might have failed
            self.checksum_sha256 = await calculate_sha256_checksum_for_bytes(self.data)
        else:
            # If data is still None here, it means read_data failed and error should be set.
            if not self.error: # Should not happen if read_data raises
                 self.error = FileProcessingError(f"Part {self.part_number} data is missing for checksum calculation after read attempt.")
            raise self.error # type: ignore


async def _upload_single_part_with_retry(
    part_task: UploadPartTask,
    api_client: ApiClient,
    config: AppConfig,
    s3_upload_id: str,
    s3_object_key: str, 
    collection: str,
    original_file_mime_type: str,
    progress: Progress,
    overall_task_id # For updating overall progress
) -> Optional[PartInfo]:
    """Uploads a single part with retries and progress updates."""
    if part_task.data is None or part_task.checksum_sha256 is None:
        logger.error(f"Part {part_task.part_number} data or checksum not prepared. Error: {part_task.error}")
        # If error is already set from read/checksum, don't overwrite with a generic UploadError
        if part_task.error:
            # Propagate the existing error if it's an Exception type
            if isinstance(part_task.error, Exception):
                 raise part_task.error
            else: # Should not happen
                 raise UploadError(f"Part {part_task.part_number} data/checksum missing and error state unclear.")
        raise UploadError(f"Part {part_task.part_number} data/checksum missing before upload attempt.")

    part_progress_desc = f"  Part {part_task.part_number:>4} ({format_bytes(part_task.size)})"
    part_rich_task_id = progress.add_task(part_progress_desc, total=part_task.size, visible=True, start=False) # Start later

    for attempt in range(config.retry_attempts + 1):
        part_task.retries = attempt
        try:
            progress.update(part_rich_task_id, description=f"{part_progress_desc} (Req URL {attempt+1})", completed=0, visible=True)
            progress.start_task(part_rich_task_id)
            
            get_url_payload = MultipartGetPartUrlRequest(
                upload_id=s3_upload_id,
                part_number=part_task.part_number,
                s3_key=s3_object_key, 
                collection=collection,
                checksum=part_task.checksum_sha256,
                content_type=original_file_mime_type
            )
            presigned_part_info = await api_client.get_presigned_url_for_part(get_url_payload)
            
            progress.update(part_rich_task_id, description=f"{part_progress_desc} (Upload {attempt+1})")
            s3_response = await api_client.upload_part_to_s3_presigned_put(
                url=str(presigned_part_info.presigned_url),
                part_data=part_task.data,
                content_length=part_task.size
            )
            
            etag = s3_response.headers.get("ETag")
            if not etag:
                raise APIRequestError("ETag not found in S3 response for part upload.")
            part_task.etag = etag.strip('"') 

            progress.update(part_rich_task_id, completed=part_task.size, description=f"{part_progress_desc} [green]Done[/green]", visible=False)
            progress.update(overall_task_id, advance=part_task.size) 
            
            return PartInfo(
                PartNumber=part_task.part_number,
                ETag=part_task.etag,
                ChecksumSHA256=part_task.checksum_sha256 
            )

        except APIRequestError as e:
            logger.warning(f"Attempt {attempt + 1} for part {part_task.part_number} failed: {e}")
            e_short = str(e).splitlines()[0][:30] + "..." if len(str(e).splitlines()[0]) > 30 else str(e).splitlines()[0]
            progress.update(part_rich_task_id, description=f"{part_progress_desc} [yellow]Retry {attempt+1} ({e_short})[/yellow]")
            if attempt >= config.retry_attempts:
                part_task.error = e
                progress.update(part_rich_task_id, description=f"{part_progress_desc} [red]Failed[/red]", visible=True) 
                return None 
            await asyncio.sleep(2**attempt) 
        except Exception as e: 
            logger.error(f"Unexpected error during part {part_task.part_number} upload, attempt {attempt+1}: {e}", exc_info=True)
            part_task.error = e
            progress.update(part_rich_task_id, description=f"{part_progress_desc} [red]Error[/red]", visible=True)
            return None 
    return None 


async def handle_multipart_upload(
    file_path: Path,
    file_size: int,
    collection: str,
    target_sub_path: Optional[str], # <<< Optional used here
    api_client: ApiClient,
    config: AppConfig,
    global_args: GlobalArgs,
    part_concurrency: int
):
    """Orchestrates the multipart upload of a large file."""
    rich_console.print(f"[info]Preparing multipart upload for: [bold cyan]{file_path.name}[/bold cyan] ({format_bytes(file_size)})")

    chunk_size_bytes = config.multipart_chunk_size_mb * (1024**2)
    if chunk_size_bytes < (5 * 1024**2): 
        logger.warning(f"Configured chunk size {config.multipart_chunk_size_mb}MB is less than S3 minimum 5MB. Adjusting to 5MB.")
        chunk_size_bytes = 5 * 1024**2
    
    num_parts = math.ceil(file_size / chunk_size_bytes) if file_size > 0 else 0

    if num_parts > S3_MAX_PARTS:
        raise UploadError(
            f"File {file_path.name} would require {num_parts} parts, exceeding S3 limit of {S3_MAX_PARTS}. "
            f"Increase multipart_chunk_size_mb in config or upload a smaller file."
        )
    if file_size > 0 and num_parts == 0 : # Should only happen if file_size is tiny and rounds down.
        # This case should ideally be handled by single file upload.
        # If file_size > 0 but less than chunk_size_bytes, num_parts will be 1.
        # If file_size is 0, num_parts will be 0.
        if file_size > 0 : # If file has content but num_parts is 0, it's an issue.
             logger.warning(f"Calculated 0 parts for a non-empty file {file_path.name} of size {file_size}. Will attempt as 1 part if logic proceeds.")
             # This case should ideally not be hit if multipart_threshold_gb is sensible.
             # If file_size is very small but still > multipart_threshold_gb, this could be an issue.

    mime_type = get_mime_type(file_path)
    rich_console.print(f"  Calculating overall SHA256 checksum for {file_path.name}...")
    overall_file_checksum_sha256 = await calculate_sha256_checksum(file_path)
    logger.info(f"Overall SHA256 for {file_path.name}: {overall_file_checksum_sha256}")

    start_payload = MultipartStartRequest(
        file_name=file_path.name,
        collection=collection,
        upload_target=target_sub_path,
        content_type=mime_type
    )
    s3_upload_id: Optional[str] = None # Initialize
    s3_object_key: Optional[str] = None # Initialize

    try:
        rich_console.print(f"  Initiating multipart upload for {file_path.name}...")
        start_response = await api_client.start_multipart_upload(start_payload)
        s3_upload_id = start_response.upload_id
        s3_object_key = start_response.s3_key 
        logger.info(f"Multipart upload initiated for {file_path.name}. Upload ID: {s3_upload_id}, S3 Key: {s3_object_key}")
    except APIRequestError as e:
        raise UploadError(f"Failed to initiate multipart upload for {file_path.name}.", original_exception=e)
    
    # Ensure s3_upload_id and s3_object_key are not None before proceeding
    if not s3_upload_id or not s3_object_key:
        raise UploadError(f"Missing s3_upload_id or s3_object_key after initiating multipart for {file_path.name}.")


    uploaded_parts_info: List[PartInfo] = []
    part_tasks_to_process: List[UploadPartTask] = []
    if num_parts > 0: # Only create parts if there are any
        for i in range(num_parts):
            part_number = i + 1
            offset = i * chunk_size_bytes
            size = min(chunk_size_bytes, file_size - offset)
            if size <= 0: continue # Should not happen if num_parts is calculated correctly for file_size > 0
            part_tasks_to_process.append(UploadPartTask(part_number, offset, size, file_path))
    
    if not part_tasks_to_process and file_size > 0: # If file has size but no tasks (e.g. num_parts was 0 incorrectly)
        raise UploadError(f"No parts to process for file {file_path.name} of size {file_size}. Check chunking logic.")
    elif not part_tasks_to_process and file_size == 0: # Uploading an empty file (if allowed by backend)
        rich_console.print(f"[yellow]  File {file_path.name} is empty. Completing multipart upload with zero parts (if backend supports).[/yellow]")
        # Proceed to complete with empty parts list. Backend must support this.

    if part_tasks_to_process:
        rich_console.print(f"  Preparing {len(part_tasks_to_process)} parts for {file_path.name}...")
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=rich_console, transient=True) as prep_progress:
            prep_task = prep_progress.add_task("Reading part data & checksums...", total=len(part_tasks_to_process))
            for part_task_item in part_tasks_to_process:
                try:
                    await part_task_item.read_data()
                    await part_task_item.calculate_checksum()
                except FileProcessingError as e:
                    logger.error(f"Failed to prepare part {part_task_item.part_number}: {e}")
                    # This part cannot be uploaded. We should abort.
                    rich_console.print(f"[bold red]  Critical error preparing part {part_task_item.part_number}. Aborting upload for {file_path.name}.[/bold red]")
                    abort_payload = MultipartAbortRequest(upload_id=s3_upload_id, s3_key=s3_object_key, collection=collection)
                    try:
                        await api_client.abort_multipart_upload(abort_payload)
                    except APIRequestError as abort_e:
                        logger.error(f"Also failed to abort multipart upload {s3_upload_id} after part prep error: {abort_e}")
                    raise UploadError(f"Failed to prepare part {part_task_item.part_number} for {file_path.name}.", original_exception=e)
                prep_progress.update(prep_task, advance=1)


    semaphore = asyncio.Semaphore(part_concurrency)
    background_tasks_set = set() # Use a set for create_task pattern
    
    with Progress(
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.1f}%"),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=rich_console
    ) as progress_bar_instance: # Renamed from 'progress'
        overall_desc = f"Uploading {file_path.name}"
        overall_task_id_rich = progress_bar_instance.add_task(overall_desc, total=file_size if file_size > 0 else 1) # Avoid total=0 for empty files
        
        async def part_worker_wrapper(pt: UploadPartTask): # Renamed from part_worker
            async with semaphore:
                part_info_result = await _upload_single_part_with_retry(
                    pt, api_client, config, s3_upload_id, s3_object_key, # type: ignore
                    collection, mime_type, progress_bar_instance, overall_task_id_rich
                )
                if part_info_result:
                    uploaded_parts_info.append(part_info_result)

        if part_tasks_to_process:
            for pt_item in part_tasks_to_process:
                task = asyncio.create_task(part_worker_wrapper(pt_item))
                background_tasks_set.add(task)
                task.add_done_callback(background_tasks_set.discard)
            
            if background_tasks_set: # Ensure there are tasks to gather
                 await asyncio.gather(*background_tasks_set, return_exceptions=False) 
        else: # Handle empty file case - advance progress if total was set to 1
            if file_size == 0:
                progress_bar_instance.update(overall_task_id_rich, completed=1)


    if part_tasks_to_process and len(uploaded_parts_info) != len(part_tasks_to_process):
        failed_parts_numbers = [pt.part_number for pt in part_tasks_to_process if pt.etag is None]
        logger.error(f"Not all parts uploaded successfully for {file_path.name}. Expected {len(part_tasks_to_process)}, got {len(uploaded_parts_info)}. Failed parts: {failed_parts_numbers}")
        rich_console.print(f"[bold red]  Failed to upload all parts for {file_path.name}. Aborting...[/bold red]")
        abort_payload = MultipartAbortRequest(upload_id=s3_upload_id, s3_key=s3_object_key, collection=collection) # type: ignore
        try:
            await api_client.abort_multipart_upload(abort_payload)
            logger.info(f"Multipart upload aborted for {file_path.name}, Upload ID: {s3_upload_id}")
        except APIRequestError as abort_e:
            logger.error(f"Failed to abort multipart upload {s3_upload_id} for {file_path.name}: {abort_e}")
        raise UploadError(f"One or more parts failed to upload for {file_path.name}. Failed parts: {failed_parts_numbers[:5]}...")


    uploaded_parts_info.sort(key=lambda p: p.PartNumber)
    
    complete_payload = MultipartCompleteRequest(
        upload_id=s3_upload_id, # type: ignore
        parts=uploaded_parts_info,
        s3_key=s3_object_key, # type: ignore
        collection=collection,
        checksum=overall_file_checksum_sha256 
    )

    try:
        rich_console.print(f"  Completing multipart upload for {file_path.name}...")
        complete_response = await api_client.complete_multipart_upload(complete_payload)
        logger.info(f"Multipart upload completed for {file_path.name}. S3 Location: {complete_response.Location}, ETag: {complete_response.ETag}")
        rich_console.print(f"[green]  Successfully uploaded and completed {file_path.name}.[/green]")
    except APIRequestError as e:
        logger.error(f"Failed to complete multipart upload for {file_path.name}: {e}. Aborting.")
        rich_console.print(f"[bold red]  Failed to complete multipart upload for {file_path.name}. Aborting...[/bold red]")
        abort_payload = MultipartAbortRequest(upload_id=s3_upload_id, s3_key=s3_object_key, collection=collection) # type: ignore
        try:
            await api_client.abort_multipart_upload(abort_payload)
            logger.info(f"Multipart upload (after failed complete) aborted for {file_path.name}, Upload ID: {s3_upload_id}")
        except APIRequestError as abort_e:
            logger.error(f"Also failed to abort multipart upload {s3_upload_id} (after failed complete) for {file_path.name}: {abort_e}")
        raise UploadError(f"Failed to complete multipart upload for {file_path.name}.", original_exception=e)

    logger.info(f"Multipart upload process completed for {file_path.name}.")
