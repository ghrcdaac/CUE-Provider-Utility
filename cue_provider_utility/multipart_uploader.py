"""
Handles the multipart upload process for large files.
"""
import asyncio
import logging
from pathlib import Path
from typing import Optional, List, Any
import math

from .models import (
    AppConfig, PartInfo,
    MultipartStartRequest, MultipartStartResponse,
    MultipartGetPartUrlRequest, MultipartGetPartUrlResponse,
    MultipartCompleteRequest, MultipartAbortRequest, UploadCompletionResponse
)
from .exceptions import UploadError, FileProcessingError, APIRequestError
from .api_client import ApiClient
from .utils import (
    calculate_sha256_checksum,
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

async def _upload_single_part_with_retry(
    part_task: UploadPartTask, api_client: ApiClient, config: AppConfig,
    s3_upload_id: str, backend_s3_key: str,
    progress: Progress, overall_task_id: Any
) -> Optional[PartInfo]:
    if part_task.data is None:
        raise UploadError(f"Part {part_task.part_number} data missing before upload attempt.")

    for attempt in range(config.retry_attempts + 1):
        try:
            get_url_payload = MultipartGetPartUrlRequest(
                s3_key=backend_s3_key,
                upload_id=s3_upload_id,
                part_number=part_task.part_number
            )
            presigned_part_info = await api_client.get_multipart_part_url(get_url_payload)
            
            s3_response = await api_client.upload_to_s3_presigned_put(
                url=str(presigned_part_info.presigned_url), file_data=part_task.data,
                content_length=part_task.size
            )
            
            etag = s3_response.headers.get("ETag")
            if not etag:
                raise APIRequestError("ETag not found in S3 response for part upload.")
            part_task.etag = etag.strip('"')

            progress.update(overall_task_id, advance=part_task.size)
            
            return PartInfo(PartNumber=part_task.part_number, ETag=part_task.etag)
        except APIRequestError as e:
            logger.warning(f"Attempt {attempt + 1} for part {part_task.part_number} failed: {str(e)}")
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
    api_client: ApiClient, config: AppConfig, part_concurrency: int,
    progress: Optional[Progress] = None,
    overall_folder_task_id: Optional[Any] = None
):
    """
    Performs a complete multipart upload transaction using the V2 API.
    """
    if progress is None:
        rich_console.print(f"[info]Preparing multipart upload for: [bold cyan]{file_path.name}[/bold cyan] ({format_bytes(file_size)})")

    chunk_size_bytes = config.multipart_chunk_size_mb * (1024**2)
    num_parts = math.ceil(file_size / chunk_size_bytes) if file_size > 0 else 0

    if num_parts > S3_MAX_PARTS:
        raise UploadError(f"File {file_path.name} requires {num_parts} parts, exceeding S3 limit of {S3_MAX_PARTS}.")

    try:
        mime_type = await get_mime_type(file_path)
        rich_console.print(f"  Calculating overall SHA256 checksum for {file_path.name}...")
        overall_file_checksum_sha256 = await calculate_sha256_checksum(file_path)
    except FileProcessingError as e:
        raise UploadError(f"File preparation failed for {file_path.name}: {e}")
    
    start_payload = MultipartStartRequest(
        file_name=file_path.name, collection_name=collection, content_type=mime_type,
        collection_path=target_sub_path
    )

    try:
        rich_console.print(f"  Initiating multipart upload with backend...")
        start_response = await api_client.start_multipart(start_payload)
        s3_upload_id, backend_s3_key = start_response.upload_id, start_response.s3_key
    except APIRequestError as e:
        raise UploadError(f"Failed to initiate multipart upload. Reason: {str(e)}")

    part_tasks_to_process: List[UploadPartTask] = [
        UploadPartTask(i + 1, i * chunk_size_bytes, min(chunk_size_bytes, file_size - (i * chunk_size_bytes)), file_path)
        for i in range(num_parts) if min(chunk_size_bytes, file_size - (i * chunk_size_bytes)) > 0
    ]
    
    read_tasks = [pt.read_data() for pt in part_tasks_to_process]
    if read_tasks:
        rich_console.print(f"  Preparing {len(read_tasks)} parts for upload...")
        await asyncio.gather(*read_tasks)

    uploaded_parts_info: List[PartInfo] = []
    
    async def run_uploads(progress_manager: Progress):
        task_id = overall_folder_task_id
        if task_id is None:
            task_id = progress_manager.add_task(f"Uploading {file_path.name}", total=file_size if file_size > 0 else 1)
        
        semaphore = asyncio.Semaphore(part_concurrency)
        async def part_worker_wrapper(pt: UploadPartTask):
            async with semaphore:
                part_info_result = await _upload_single_part_with_retry(
                    pt, api_client, config, s3_upload_id, backend_s3_key,
                    progress_manager, task_id
                )
                if part_info_result:
                    uploaded_parts_info.append(part_info_result)

        upload_tasks = [part_worker_wrapper(pt) for pt in part_tasks_to_process]
        if upload_tasks:
            await asyncio.gather(*upload_tasks)
        elif file_size == 0:
            progress_manager.update(task_id, completed=1)

    if progress is None:
        # Standalone mode: create and manage its own Progress context
        with Progress(
            SpinnerColumn(), TextColumn("[bold cyan]{task.description}"), BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.1f}%"),
            TransferSpeedColumn(), TimeRemainingColumn(), console=rich_console
        ) as standalone_progress:
            await run_uploads(standalone_progress)
    else:
        # Folder mode: use the provided progress object
        await run_uploads(progress)

    if len(uploaded_parts_info) != len(part_tasks_to_process):
        failed_parts_numbers = [pt.part_number for pt in part_tasks_to_process if pt.etag is None]
        first_error = next((pt.error for pt in part_tasks_to_process if pt.error is not None), "Unknown error")
        logger.error(f"Not all parts uploaded successfully for {file_path.name}. Failed parts: {failed_parts_numbers}")
        rich_console.print(f"[bold red]  Failed to upload all parts. Aborting with backend...[/bold red]")
        abort_payload = MultipartAbortRequest(upload_id=s3_upload_id, s3_key=backend_s3_key)
        try:
            await api_client.abort_multipart(abort_payload)
        except APIRequestError as abort_e:
            logger.error(f"Also failed to abort multipart upload {s3_upload_id}: {str(abort_e)}")
        raise UploadError(f"One or more parts failed to upload. First error: {first_error}")

    uploaded_parts_info.sort(key=lambda p: p.PartNumber)
    
    complete_payload = MultipartCompleteRequest(
        upload_id=s3_upload_id, parts=uploaded_parts_info, s3_key=backend_s3_key, 
        file_name=file_path.name, collection_name=collection, checksum=overall_file_checksum_sha256,
        final_file_size=file_size, collection_path=target_sub_path, content_type=mime_type
    )

    try:
        rich_console.print(f"  Completing multipart upload with backend...")
        completion_response = await api_client.complete_multipart(complete_payload)
        rich_console.print(f"[green]  Successfully uploaded and completed {file_path.name}. Status: {completion_response.status}[/green]")
    except APIRequestError as e:
        logger.error(f"Failed to complete multipart upload for {file_path.name}: {e}. Aborting S3 MPU.")
        rich_console.print(f"[bold red]  Failed to complete multipart upload. Aborting S3 MPU...[/bold red]")
        abort_payload_on_failed_complete = MultipartAbortRequest(upload_id=s3_upload_id, s3_key=backend_s3_key)
        try:
            await api_client.abort_multipart(abort_payload_on_failed_complete)
        except APIRequestError as abort_e:
            logger.error(f"Also failed to abort S3 multipart upload {s3_upload_id}: {str(abort_e)}")
        raise UploadError(f"Failed to complete multipart upload. Reason: {str(e)}")