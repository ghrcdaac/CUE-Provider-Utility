"""
Handles the multipart upload process for large files.
Coordinates with ApiClient to:
1. Initiate multipart upload (gets s3_key and S3 UploadId from backend).
2. Upload parts concurrently to S3.
3. Complete the multipart upload with the backend (which then creates DB records).
4. Abort with backend if necessary (which then aborts with S3).
"""
import asyncio
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
import math

from .models import (
    AppConfig, GlobalArgs, PartInfo,
    MultipartStartRequest, MultipartStartResponse,
    MultipartGetPartUrlRequest, MultipartGetPartUrlResponse,
    MultipartCompleteRequest, MultipartAbortRequest
)
from .exceptions import UploadError, FileProcessingError, APIRequestError
from .api_client import ApiClient
from .utils import (
    calculate_sha256_checksum, calculate_sha256_checksum_for_bytes,
    get_mime_type, format_bytes
)
from .logger_setup import rich_console
from rich.progress import Progress, BarColumn, TextColumn, TransferSpeedColumn, TimeRemainingColumn, SpinnerColumn
import aiofiles

logger = logging.getLogger(__name__)

S3_MAX_PARTS = 10000

class UploadPartTask:
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
        try:
            async with aiofiles.open(self.file_path, "rb") as f:
                await f.seek(self.offset)
                self.data = await f.read(self.size)
            if len(self.data) != self.size: 
                self.error = FileProcessingError(f"Read incorrect amount of data for part {self.part_number}. Expected {self.size}, got {len(self.data)}")
                raise self.error 
        except OSError as e:
            self.error = FileProcessingError(f"Error reading data for part {self.part_number}", original_exception=e)
            raise self.error 

    async def calculate_checksum(self):
        if self.data is None: 
            logger.warning(f"Part {self.part_number} data was None before checksum calculation. Attempting read.")
            await self.read_data() 
        
        if self.data: 
            self.checksum_sha256 = await calculate_sha256_checksum_for_bytes(self.data)
        else:
            if not self.error: 
                 self.error = FileProcessingError(f"Part {self.part_number} data is missing for checksum calculation after read attempt.")
            raise self.error 


async def _upload_single_part_with_retry(
    part_task: UploadPartTask, api_client: ApiClient, config: AppConfig,
    s3_upload_id: str, backend_s3_key: str, # Renamed for clarity (this is the key from /start)
    collection: str, original_file_mime_type: str,
    progress: Progress, overall_task_id 
) -> Optional[PartInfo]:
    if part_task.data is None or part_task.checksum_sha256 is None:
        logger.error(f"Part {part_task.part_number} data or checksum not prepared. Error: {part_task.error}")
        if part_task.error and isinstance(part_task.error, Exception):
             raise part_task.error
        raise UploadError(f"Part {part_task.part_number} data/checksum missing before upload attempt.")

    part_progress_desc = f"  Part {part_task.part_number:>4} ({format_bytes(part_task.size)})"
    part_rich_task_id = progress.add_task(part_progress_desc, total=part_task.size, visible=True, start=False)

    for attempt in range(config.retry_attempts + 1):
        part_task.retries = attempt
        try:
            progress.update(part_rich_task_id, description=f"{part_progress_desc} (Req URL {attempt+1})", completed=0, visible=True)
            progress.start_task(part_rich_task_id)
            
            get_url_payload = MultipartGetPartUrlRequest(
                upload_id=s3_upload_id, part_number=part_task.part_number,
                file_name=backend_s3_key, # Client sends s3_key as 'file_name' to backend
                collection=collection,
                checksum=part_task.checksum_sha256, content_type=original_file_mime_type
            )
            presigned_part_info = await api_client.get_presigned_url_for_part(get_url_payload)
            
            progress.update(part_rich_task_id, description=f"{part_progress_desc} (Upload {attempt+1})")
            s3_response = await api_client.upload_part_to_s3_presigned_put(
                url=str(presigned_part_info.presigned_url), part_data=part_task.data,
                content_length=part_task.size
            )
            
            etag = s3_response.headers.get("ETag")
            if not etag:
                raise APIRequestError("ETag not found in S3 response for part upload.")
            part_task.etag = etag.strip('"') 

            progress.update(part_rich_task_id, completed=part_task.size, description=f"{part_progress_desc} [green]Done[/green]", visible=False)
            progress.update(overall_task_id, advance=part_task.size) 
            
            return PartInfo(
                PartNumber=part_task.part_number, ETag=part_task.etag,
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
    file_path: Path, file_size: int, collection: str, target_sub_path: Optional[str], # target_sub_path is collection_path for client model
    api_client: ApiClient, config: AppConfig, global_args: GlobalArgs, part_concurrency: int
):
    rich_console.print(f"[info]Preparing multipart upload for: [bold cyan]{file_path.name}[/bold cyan] ({format_bytes(file_size)})")

    chunk_size_bytes = config.multipart_chunk_size_mb * (1024**2)
    if chunk_size_bytes < (5 * 1024**2): 
        logger.warning(f"Configured chunk size {config.multipart_chunk_size_mb}MB is less than S3 minimum 5MB. Adjusting to 5MB.")
        chunk_size_bytes = 5 * 1024**2
    
    num_parts = math.ceil(file_size / chunk_size_bytes) if file_size > 0 else 0

    if num_parts > S3_MAX_PARTS:
        raise UploadError(f"File {file_path.name} requires {num_parts} parts, exceeding S3 limit of {S3_MAX_PARTS}.")

    mime_type = get_mime_type(file_path)
    rich_console.print(f"  Calculating overall SHA256 checksum for {file_path.name}...")
    overall_file_checksum_sha256 = await calculate_sha256_checksum(file_path)
    logger.info(f"Overall SHA256 for {file_path.name}: {overall_file_checksum_sha256}")

    start_payload = MultipartStartRequest(
        file_name=file_path.name, 
        collection=collection,
        upload_target=target_sub_path, # This is the user's desired sub-path in collection
        content_type=mime_type,
        overall_checksum=overall_file_checksum_sha256
    )
    s3_upload_id: Optional[str] = None 
    # This is the key generated by the backend, which the client will use for S3 operations
    # and for identifying the file to the backend in complete/abort.
    backend_s3_key: Optional[str] = None 

    try:
        rich_console.print(f"  Initiating multipart upload with backend for {file_path.name}...")
        start_response = await api_client.start_multipart_upload(start_payload)
        s3_upload_id = start_response.upload_id
        backend_s3_key = start_response.s3_key # Backend MUST provide this now
            
        logger.info(f"Multipart initiated with backend. S3 Upload ID: {s3_upload_id}, Backend S3 Key: {backend_s3_key}")
    except APIRequestError as e:
        raise UploadError(f"Failed to initiate multipart upload for {file_path.name}.", original_exception=e)
    
    if not s3_upload_id or not backend_s3_key: # Should always have backend_s3_key now
        raise UploadError(f"Missing S3 UploadId or Backend S3 Key after initiating multipart for {file_path.name}.")

    uploaded_parts_info: List[PartInfo] = []
    part_tasks_to_process: List[UploadPartTask] = []
    if num_parts > 0: 
        for i in range(num_parts):
            part_number = i + 1; offset = i * chunk_size_bytes
            size = min(chunk_size_bytes, file_size - offset)
            if size <= 0: continue 
            part_tasks_to_process.append(UploadPartTask(part_number, offset, size, file_path))
    
    if not part_tasks_to_process and file_size > 0: 
        raise UploadError(f"No parts to process for file {file_path.name} of size {file_size}.")
    elif not part_tasks_to_process and file_size == 0: 
        rich_console.print(f"[yellow]  File {file_path.name} is empty. Proceeding to complete multipart upload with zero parts.[/yellow]")

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
                    rich_console.print(f"[bold red]  Critical error preparing part {part_task_item.part_number}. Aborting upload for {file_path.name}.[/bold red]")
                    # Abort with backend (which then aborts with S3)
                    abort_payload = MultipartAbortRequest(
                        upload_id=s3_upload_id, s3_key=backend_s3_key, 
                        file_name=backend_s3_key, # Backend expects file_name, use s3_key
                        collection=collection
                    )
                    try:
                        await api_client.abort_multipart_upload(abort_payload)
                        logger.info(f"Multipart upload aborted with backend due to part prep error. Upload ID: {s3_upload_id}")
                    except APIRequestError as abort_e:
                        logger.error(f"Also failed to abort multipart upload {s3_upload_id} with backend after part prep error: {abort_e}")
                    raise UploadError(f"Failed to prepare part {part_task_item.part_number} for {file_path.name}.", original_exception=e)
                prep_progress.update(prep_task, advance=1)


    semaphore = asyncio.Semaphore(part_concurrency)
    background_tasks_set = set() 
    
    with Progress(
        SpinnerColumn(), TextColumn("[bold cyan]{task.description}"), BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.1f}%"),
        TransferSpeedColumn(), TimeRemainingColumn(), console=rich_console
    ) as progress_bar_instance: 
        overall_desc = f"Uploading {file_path.name}"
        overall_task_id_rich = progress_bar_instance.add_task(overall_desc, total=file_size if file_size > 0 else 1) 
        
        async def part_worker_wrapper(pt: UploadPartTask): 
            async with semaphore:
                part_info_result = await _upload_single_part_with_retry(
                    pt, api_client, config, s3_upload_id, 
                    backend_s3_key,  
                    collection, mime_type, progress_bar_instance, overall_task_id_rich
                )
                if part_info_result:
                    uploaded_parts_info.append(part_info_result)

        if part_tasks_to_process:
            for pt_item in part_tasks_to_process:
                task = asyncio.create_task(part_worker_wrapper(pt_item))
                background_tasks_set.add(task)
                task.add_done_callback(background_tasks_set.discard)
            
            if background_tasks_set: 
                 await asyncio.gather(*background_tasks_set, return_exceptions=False) 
        else: 
            if file_size == 0: # For empty file, mark progress as complete
                progress_bar_instance.update(overall_task_id_rich, completed=1)

    if part_tasks_to_process and len(uploaded_parts_info) != len(part_tasks_to_process):
        failed_parts_numbers = [pt.part_number for pt in part_tasks_to_process if pt.etag is None]
        logger.error(f"Not all parts uploaded successfully for {file_path.name}. Failed parts: {failed_parts_numbers}")
        rich_console.print(f"[bold red]  Failed to upload all parts for {file_path.name}. Aborting with backend...[/bold red]")
        abort_payload = MultipartAbortRequest(
            upload_id=s3_upload_id, 
            s3_key=backend_s3_key,  
            file_name=backend_s3_key,  
            collection=collection
        )
        try:
            await api_client.abort_multipart_upload(abort_payload)
            logger.info(f"Multipart upload aborted with backend for {file_path.name}, Upload ID: {s3_upload_id}")
        except APIRequestError as abort_e:
            logger.error(f"Failed to abort multipart upload {s3_upload_id} with backend for {file_path.name}: {abort_e}")
        raise UploadError(f"One or more parts failed to upload for {file_path.name}. Failed parts: {failed_parts_numbers[:5]}...")

    uploaded_parts_info.sort(key=lambda p: p.PartNumber)
    
    complete_payload = MultipartCompleteRequest(
        upload_id=s3_upload_id, 
        parts=uploaded_parts_info,
        s3_key=backend_s3_key, 
        file_name=file_path.name, # Send original local filename for DB record
        collection=collection,
        checksum=overall_file_checksum_sha256,
        final_file_size=file_size, # Send the actual file size
        collection_path=target_sub_path, # Send user's target sub-path
        content_type=mime_type # Send original content type
    )

    try:
        rich_console.print(f"  Completing multipart upload with backend for {file_path.name}...")
        complete_response = await api_client.complete_multipart_upload(complete_payload)
        logger.info(f"Multipart upload completed with backend for {file_path.name}. S3 Location: {complete_response.Location}, ETag: {complete_response.ETag}")
        rich_console.print(f"[green]  Successfully uploaded and completed {file_path.name}.[/green]")
    except APIRequestError as e:
        logger.error(f"Failed to complete multipart upload with backend for {file_path.name}: {e}. Aborting S3 MPU via backend.")
        rich_console.print(f"[bold red]  Failed to complete multipart upload with backend for {file_path.name}. Aborting S3 MPU via backend...[/bold red]")
        abort_payload_on_failed_complete = MultipartAbortRequest(
            upload_id=s3_upload_id, 
            s3_key=backend_s3_key, 
            file_name=backend_s3_key,  
            collection=collection
        )
        try:
            await api_client.abort_multipart_upload(abort_payload_on_failed_complete)
            logger.info(f"S3 Multipart upload (after failed complete) aborted via backend for {file_path.name}, Upload ID: {s3_upload_id}")
        except APIRequestError as abort_e:
            logger.error(f"Also failed to abort S3 multipart upload {s3_upload_id} via backend (after failed complete) for {file_path.name}: {abort_e}")
        raise UploadError(f"Failed to complete multipart upload with backend for {file_path.name}.", original_exception=e)

    logger.info(f"Multipart upload process completed for {file_path.name}.")

