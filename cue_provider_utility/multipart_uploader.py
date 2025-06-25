"""
Handles the multipart upload process for large files.
"""
import asyncio
import logging
from pathlib import Path
from typing import Optional, List, Any
import math

from .models import (
    AppConfig, GlobalArgs, PartInfo,
    MultipartStartRequest, MultipartStartResponse,
    MultipartGetPartUrlRequest, MultipartGetPartUrlResponse,
    MultipartCompleteRequest, MultipartCompleteResponse, MultipartAbortRequest
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
                self.error = FileProcessingError(f"Read incorrect amount of data for part {self.part_number}.")
                raise self.error 
        except OSError as e:
            self.error = FileProcessingError(f"Error reading data for part {self.part_number}", original_exception=e)
            raise self.error 

    async def calculate_checksum(self):
        if self.data is None: 
            await self.read_data()
        
        if self.data: 
            self.checksum_sha256 = await calculate_sha256_checksum_for_bytes(self.data)
        elif not self.error: 
            self.error = FileProcessingError(f"Part {self.part_number} data is missing for checksum calculation.")
            raise self.error 


async def _upload_single_part_with_retry(
    part_task: UploadPartTask, api_client: ApiClient, config: AppConfig,
    s3_upload_id: str, backend_s3_key: str,
    collection: str, original_file_mime_type: str,
    progress: Progress, overall_task_id: Any
) -> Optional[PartInfo]:
    if part_task.data is None or part_task.checksum_sha256 is None:
        # This will be caught by the calling function and reported correctly.
        raise UploadError(f"Part {part_task.part_number} data/checksum missing before upload attempt.")

    # Hide the individual part progress bars from the main console to reduce noise.
    # They will still be used internally to track progress.
    part_rich_task_id = progress.add_task(f"Part {part_task.part_number}", total=part_task.size, visible=False)
    progress.start_task(part_rich_task_id)

    for attempt in range(config.retry_attempts + 1):
        part_task.retries = attempt
        try:
            get_url_payload = MultipartGetPartUrlRequest(
                upload_id=s3_upload_id, part_number=part_task.part_number,
                file_name=backend_s3_key, collection=collection,
                checksum=part_task.checksum_sha256, content_type=original_file_mime_type
            )
            presigned_part_info = await api_client.get_presigned_url_for_part(get_url_payload)
            
            s3_response = await api_client.upload_part_to_s3_presigned_put(
                url=str(presigned_part_info.presigned_url), part_data=part_task.data,
                content_length=part_task.size
            )
            
            etag = s3_response.headers.get("ETag")
            if not etag:
                raise APIRequestError("ETag not found in S3 response for part upload.")
            part_task.etag = etag.strip('"') 

            progress.update(overall_task_id, advance=part_task.size) 
            progress.update(part_rich_task_id, completed=part_task.size)
            
            return PartInfo(
                PartNumber=part_task.part_number, ETag=part_task.etag,
                ChecksumSHA256=part_task.checksum_sha256 
            )
        except APIRequestError as e:
            logger.warning(f"Attempt {attempt + 1} for part {part_task.part_number} failed: {e}")
            if attempt >= config.retry_attempts:
                part_task.error = e
                return None 
            await asyncio.sleep(2**attempt) 
        except Exception as e: 
            logger.error(f"Unexpected error during part {part_task.part_number} upload, attempt {attempt+1}: {e}", exc_info=True)
            part_task.error = e
            return None 
    return None 


async def handle_multipart_upload(
    file_path: Path, file_size: int, collection: str, target_sub_path: Optional[str],
    api_client: ApiClient, config: AppConfig, global_args: GlobalArgs, part_concurrency: int
):
    rich_console.print(f"[info]Preparing multipart upload for: [bold cyan]{file_path.name}[/bold cyan] ({format_bytes(file_size)})")

    chunk_size_bytes = config.multipart_chunk_size_mb * (1024**2)
    if chunk_size_bytes < (5 * 1024**2): 
        chunk_size_bytes = 5 * 1024**2
    num_parts = math.ceil(file_size / chunk_size_bytes) if file_size > 0 else 0

    if num_parts > S3_MAX_PARTS:
        raise UploadError(f"File {file_path.name} requires {num_parts} parts, exceeding S3 limit of {S3_MAX_PARTS}.")

    mime_type = await get_mime_type(file_path)
    rich_console.print(f"  Calculating overall SHA256 checksum for {file_path.name}...")
    overall_file_checksum_sha256 = await calculate_sha256_checksum(file_path)
    
    start_payload = MultipartStartRequest(
        file_name=file_path.name, collection=collection, upload_target=target_sub_path,
        content_type=mime_type, overall_checksum=overall_file_checksum_sha256
    )
    
    try:
        rich_console.print(f"  Initiating multipart upload with backend...")
        start_response = await api_client.start_multipart_upload(start_payload)
        s3_upload_id, backend_s3_key = start_response.upload_id, start_response.s3_key
    except APIRequestError as e:
        # FIX: Propagate the specific error message from the API
        raise UploadError(f"Failed to initiate multipart upload. Reason: {e}")

    part_tasks_to_process: List[UploadPartTask] = [
        UploadPartTask(i + 1, i * chunk_size_bytes, min(chunk_size_bytes, file_size - (i * chunk_size_bytes)), file_path)
        for i in range(num_parts) if min(chunk_size_bytes, file_size - (i * chunk_size_bytes)) > 0
    ]
    
    if part_tasks_to_process:
        rich_console.print(f"  Preparing {len(part_tasks_to_process)} parts...")
        for part_task in part_tasks_to_process:
            await part_task.read_data()
            await part_task.calculate_checksum()

    uploaded_parts_info: List[PartInfo] = []
    
    with Progress(
        SpinnerColumn(), TextColumn("[bold cyan]{task.description}"), BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.1f}%"),
        TransferSpeedColumn(), TimeRemainingColumn(), console=rich_console
    ) as progress:
        overall_desc = f"Uploading {file_path.name}"
        overall_task_id = progress.add_task(overall_desc, total=file_size if file_size > 0 else 1) 
        
        semaphore = asyncio.Semaphore(part_concurrency)
        async def part_worker_wrapper(pt: UploadPartTask): 
            async with semaphore:
                part_info_result = await _upload_single_part_with_retry(
                    pt, api_client, config, s3_upload_id, backend_s3_key, 
                    collection, mime_type, progress, overall_task_id
                )
                if part_info_result:
                    uploaded_parts_info.append(part_info_result)

        upload_tasks = [part_worker_wrapper(pt) for pt in part_tasks_to_process]
        if upload_tasks:
            await asyncio.gather(*upload_tasks)
        elif file_size == 0:
            progress.update(overall_task_id, completed=1)

    if len(uploaded_parts_info) != len(part_tasks_to_process):
        failed_parts_numbers = [pt.part_number for pt in part_tasks_to_process if pt.etag is None]
        first_error = next((pt.error for pt in part_tasks_to_process if pt.error is not None), "Unknown error")
        logger.error(f"Not all parts uploaded successfully for {file_path.name}. Failed parts: {failed_parts_numbers}")
        rich_console.print(f"[bold red]  Failed to upload all parts. Aborting with backend...[/bold red]")
        abort_payload = MultipartAbortRequest(upload_id=s3_upload_id, s3_key=backend_s3_key, file_name=backend_s3_key, collection=collection)
        try:
            await api_client.abort_multipart_upload(abort_payload)
        except APIRequestError as abort_e:
            logger.error(f"Also failed to abort multipart upload {s3_upload_id}: {abort_e}")
        # FIX: Propagate the specific error from the first failing part
        raise UploadError(f"One or more parts failed to upload. First error: {first_error}")

    uploaded_parts_info.sort(key=lambda p: p.PartNumber)
    
    complete_payload = MultipartCompleteRequest(
        upload_id=s3_upload_id, parts=uploaded_parts_info, s3_key=backend_s3_key, 
        file_name=file_path.name, collection=collection, checksum=overall_file_checksum_sha256,
        final_file_size=file_size, collection_path=target_sub_path, content_type=mime_type
    )

    try:
        rich_console.print(f"  Completing multipart upload with backend...")
        complete_response = await api_client.complete_multipart_upload(complete_payload)
        logger.info(f"Multipart upload completed for {file_path.name}. Location: {complete_response.Location}")
        rich_console.print(f"[green]  Successfully uploaded and completed {file_path.name}.[/green]")
    except APIRequestError as e:
        logger.error(f"Failed to complete multipart upload for {file_path.name}: {e}. Aborting S3 MPU.")
        rich_console.print(f"[bold red]  Failed to complete multipart upload. Aborting S3 MPU...[/bold red]")
        abort_payload_on_failed_complete = MultipartAbortRequest(upload_id=s3_upload_id, s3_key=backend_s3_key, file_name=backend_s3_key, collection=collection)
        try:
            await api_client.abort_multipart_upload(abort_payload_on_failed_complete)
        except APIRequestError as abort_e:
            logger.error(f"Also failed to abort S3 multipart upload {s3_upload_id}: {abort_e}")
        # FIX: Propagate the specific error message
        raise UploadError(f"Failed to complete multipart upload. Reason: {e}")
