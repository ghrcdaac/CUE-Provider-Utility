import asyncio
import logging
from pathlib import Path
from typing import Optional, List, Any
import math

import httpx
from .models import (
    AppConfig, PartInfo,
    MultipartStartRequest, MultipartCompleteRequest, MultipartAbortRequest,
    MultipartGetPartUrlRequest
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
    """Represents a single part of a multipart upload, handling its own read and upload logic."""
    def __init__(self, part_number: int, offset: int, size: int, file_path: Path):
        self.part_number = part_number
        self.offset = offset
        self.size = size
        self.file_path = file_path
        self.etag: Optional[str] = None
        self.error: Optional[Exception] = None

    async def read_and_upload(
        self, api_client: ApiClient, config: AppConfig, s3_upload_id: str, file_id: str,
        progress: Progress, overall_task_id: Any
    ) -> Optional[PartInfo]:
        """
        Reads this part's data from the file and uploads it, with retries.
        This is a memory-efficient, just-in-time approach.
        """
        try:
            # Read data just before uploading
            async with aiofiles.open(self.file_path, "rb") as f:
                await f.seek(self.offset)
                data = await f.read(self.size)
            if len(data) != self.size:
                raise FileProcessingError(f"Read incorrect amount of data for part {self.part_number}.")

            try:
                get_url_payload = MultipartGetPartUrlRequest(
                    file_id=file_id,
                    upload_id=s3_upload_id,
                    part_number=self.part_number
                )
                presigned_url_response = await api_client.get_multipart_part_url(get_url_payload)
                
                async with httpx.AsyncClient(timeout=None) as s3_client:
                    s3_response = await s3_client.put(str(presigned_url_response.presigned_url), content=data, headers={'Content-Length': str(self.size)})
                
                s3_response.raise_for_status()
                etag = s3_response.headers.get("ETag")
                if not etag:
                    raise APIRequestError("ETag not found in S3 response for part upload.")
                
                self.etag = etag.strip('"')
                progress.update(overall_task_id, advance=self.size)
                return PartInfo(PartNumber=self.part_number, ETag=self.etag)
            
            except (APIRequestError, httpx.HTTPError) as e:
                logger.warning(f"Upload for part {self.part_number} failed: {e}")
                self.error = e
                return None
        except Exception as e:
            logger.error(f"Unexpected error during part {self.part_number} processing: {e}", exc_info=True)
            self.error = e
            return None


async def _run_all_part_uploads(
    part_tasks: List[UploadPartTask], api_client: ApiClient, config: AppConfig,
    part_concurrency: int, s3_upload_id: str, file_id: str,
    progress: Progress, task_id: Any
) -> List[PartInfo]:
    """Manages the concurrent execution of all part uploads."""
    semaphore = asyncio.Semaphore(part_concurrency)
    async def worker(part_task: UploadPartTask) -> Optional[PartInfo]:
        async with semaphore:
            return await part_task.read_and_upload(api_client, config, s3_upload_id, file_id, progress, task_id)
    
    # Run all worker tasks concurrently
    results = await asyncio.gather(*(worker(pt) for pt in part_tasks))
    # Filter out any failures (which return None)
    return [res for res in results if res is not None]


async def handle_multipart_upload(
    file_path: Path, file_size: int, collection: str, target_sub_path: Optional[str],
    api_client: ApiClient, config: AppConfig, part_concurrency: int,
    progress: Optional[Progress] = None,
    file_task_id: Optional[Any] = None,
    cached_checksum: Optional[str] = None,
    cached_content_type: Optional[str] = None,
) -> tuple[str, str]:
    """
    Performs a complete, optimized multipart upload. Returns (checksum, mime_type).
    """
    if not progress:
        rich_console.print(f"[info]Preparing multipart upload for: [bold cyan]{file_path.name}[/bold cyan] ({format_bytes(file_size)})")

    chunk_size_bytes = config.multipart_chunk_size_mb * (1024**2)
    num_parts = math.ceil(file_size / chunk_size_bytes) if file_size > 0 else 0
    if num_parts > S3_MAX_PARTS:
        raise UploadError(f"File {file_path.name} requires {num_parts} parts, exceeding S3 limit of {S3_MAX_PARTS}.")

    if cached_content_type:
        mime_type = cached_content_type
    else:
        if progress is not None and file_task_id is not None:
            progress.update(file_task_id, description="[bold green]Determining file type...[/]")
            mime_type = await get_mime_type(file_path)
        else:
            with rich_console.status("[bold green]Determining file type...[/]"):
                mime_type = await get_mime_type(file_path)

    start_payload = MultipartStartRequest(file_name=file_path.name, collection_name=collection, content_type=mime_type, collection_path=target_sub_path)
    
    if progress is not None and file_task_id is not None:
        progress.update(file_task_id, description="[bold green]Initiating upload...[/]")
        start_response = await api_client.start_multipart(start_payload)
    else:
        with rich_console.status("[bold green]Initiating multipart upload with backend...[/]"):
            start_response = await api_client.start_multipart(start_payload)
   
    s3_upload_id = start_response.upload_id
    file_id = start_response.file_id
    part_tasks = [UploadPartTask(i + 1, i * chunk_size_bytes, min(chunk_size_bytes, file_size - (i * chunk_size_bytes)), file_path) for i in range(num_parts)]
    
    checksum_result = None
    upload_result = None

    if cached_checksum:
        checksum_result = cached_checksum
        # Run part uploads directly
        if progress is not None and file_task_id is not None:
            prog = progress
            upload_progress_task_id = file_task_id
            prog.reset(upload_progress_task_id, total=file_size, description=f"Uploading {file_path.name}")
            upload_result = await _run_all_part_uploads(part_tasks, api_client, config, part_concurrency, s3_upload_id, str(file_id), prog, upload_progress_task_id)
        else:
            with Progress(SpinnerColumn(), TextColumn("[bold cyan]{task.description}"), BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.1f}%"), TransferSpeedColumn(), TimeRemainingColumn(), console=rich_console) as prog:
                upload_progress_task_id = prog.add_task(f"Uploading {file_path.name}", total=file_size)
                upload_result = await _run_all_part_uploads(part_tasks, api_client, config, part_concurrency, s3_upload_id, str(file_id), prog, upload_progress_task_id)
    else:
        if progress is not None and file_task_id is not None:
            prog = progress
            upload_progress_task_id = file_task_id
            checksum_progress_task_id = prog.add_task(f"Checksum {file_path.name}", total=file_size)
            prog.reset(upload_progress_task_id, total=file_size, description=f"Uploading {file_path.name}")

            checksum_task = asyncio.create_task(
                calculate_sha256_checksum(file_path, progress=prog, task_id=checksum_progress_task_id)
            )
            upload_task = asyncio.create_task(_run_all_part_uploads(part_tasks, api_client, config, part_concurrency, s3_upload_id, str(file_id), prog, upload_progress_task_id))
            
            results = await asyncio.gather(checksum_task, upload_task, return_exceptions=True)
            checksum_result, upload_result = results
            prog.remove_task(checksum_progress_task_id)
        else:
            with Progress(SpinnerColumn(), TextColumn("[bold cyan]{task.description}"), BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.1f}%"), TransferSpeedColumn(), TimeRemainingColumn(), console=rich_console) as prog:
                upload_progress_task_id = prog.add_task(f"Uploading {file_path.name}", total=file_size)
                checksum_progress_task_id = prog.add_task("Calculating Checksum", total=file_size)

                checksum_task = asyncio.create_task(
                    calculate_sha256_checksum(file_path, progress=prog, task_id=checksum_progress_task_id)
                )
                upload_task = asyncio.create_task(_run_all_part_uploads(part_tasks, api_client, config, part_concurrency, s3_upload_id, str(file_id), prog, upload_progress_task_id))
                
                results = await asyncio.gather(checksum_task, upload_task, return_exceptions=True)
                checksum_result, upload_result = results

    if isinstance(checksum_result, BaseException):
        raise UploadError("Failed to calculate file checksum.", original_exception=checksum_result)
    if isinstance(upload_result, BaseException):
        raise UploadError("An unexpected error occurred during part uploads.", original_exception=upload_result)

    if not isinstance(checksum_result, str) or not isinstance(upload_result, list):
        raise UploadError("Invalid result type from multipart upload background tasks.")
            
    overall_file_checksum_sha256: str = checksum_result
    uploaded_parts_info: List[PartInfo] = upload_result

    if len(uploaded_parts_info) != num_parts:
        first_error = next((pt.error for pt in part_tasks if pt.error), "Unknown error")
        rich_console.print("[bold red]   Not all parts uploaded successfully. Aborting...[/bold red]")
        await api_client.abort_multipart(MultipartAbortRequest(upload_id=s3_upload_id, file_id=file_id))
        raise UploadError(f"One or more parts failed to upload. First error: {first_error}")

    uploaded_parts_info.sort(key=lambda p: p.PartNumber)
    complete_payload = MultipartCompleteRequest(
        file_id=file_id, upload_id=s3_upload_id, parts=uploaded_parts_info,
        file_name=file_path.name, collection_name=collection, checksum=overall_file_checksum_sha256,
        final_file_size=file_size, collection_path=target_sub_path, content_type=mime_type
    )

    if progress is not None and file_task_id is not None:
        progress.update(file_task_id, description="[bold green]Completing upload...[/]")
        await api_client.complete_multipart(complete_payload)
    else:
        with rich_console.status("[bold green]Completing multipart upload with backend...[/]"):
            await api_client.complete_multipart(complete_payload)
   
    if not progress:
        rich_console.print(f"   [green]✓[/green] Successfully uploaded [bold cyan]{file_path.name}[/bold cyan].")
    return overall_file_checksum_sha256, mime_type