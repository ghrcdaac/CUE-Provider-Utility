# In cue_provider_utility/single_file_uploader.py
# (This is the complete updated file content)

"""
Handles the process for uploading a single file (non-multipart).
"""
import logging
from pathlib import Path
from typing import Optional, Any
import asyncio

import aiofiles 
import httpx

from .models import AppConfig, PrepareSingleRequest, PrepareSingleResponse, CompleteSingleRequest, UploadCompletionResponse
from .exceptions import UploadError, FileProcessingError, APIRequestError
from .api_client import ApiClient
from .utils import calculate_sha256_checksum, get_mime_type, format_bytes
from .logger_setup import rich_console
from rich.progress import Progress, BarColumn, TextColumn, TransferSpeedColumn, TimeRemainingColumn

logger = logging.getLogger(__name__)

async def handle_single_file_upload( 
    file_path: Path,
    file_size: int,
    collection: str,
    target_sub_path: Optional[str],
    api_client: ApiClient,
    config: AppConfig,
    progress: Optional[Progress] = None,
    overall_folder_task_id: Optional[Any] = None
):
    """
    Performs a complete single file upload transaction using the V2 API.
    """
    if progress is None:
        rich_console.print(f"[info]Preparing single file upload for: [bold cyan]{file_path.name}[/bold cyan] ({format_bytes(file_size)})")

    try:
        rich_console.print(f"  Calculating SHA256 checksum for {file_path.name}...")
        checksum_sha256 = await calculate_sha256_checksum(file_path)
        mime_type = await get_mime_type(file_path)
    except FileProcessingError as e:
        raise UploadError(f"File preparation failed for {file_path.name}: {e}", original_exception=e)

    prepare_payload = PrepareSingleRequest(
        file_name=file_path.name, collection_name=collection,
        file_size_bytes=file_size, checksum=checksum_sha256,
        content_type=mime_type, collection_path=target_sub_path
    )
    
    prepare_response: Optional[PrepareSingleResponse] = None
    for attempt in range(config.retry_attempts + 1):
        try:
            attempt_msg = f" (attempt {attempt + 1}/{config.retry_attempts + 1})" if attempt > 0 else ""
            rich_console.print(f"  Requesting upload URL for {file_path.name}{attempt_msg}...")
            prepare_response = await api_client.prepare_single_upload(prepare_payload)
            break
        except APIRequestError as e:
            logger.warning(f"Attempt {attempt + 1} to get prepare-single response failed: {str(e)}")
            if attempt >= config.retry_attempts:
                raise UploadError(f"Failed to prepare single upload for {file_path.name}. Reason: {str(e)}")
            await asyncio.sleep(2**attempt)
    
    if prepare_response is None: 
        raise UploadError(f"Could not prepare single file upload for {file_path.name} after retries.")

    rich_console.print(f"  Uploading {file_path.name} to S3...")
    try:
        async with aiofiles.open(file_path, 'rb') as f:
            file_data = await f.read()
        
        s3_headers = {'Content-Type': mime_type, 'Content-Length': str(file_size)}

        async with httpx.AsyncClient(timeout=None) as client:
            if progress is None:
                with Progress(
                    TextColumn("[progress.description]{task.description}"), BarColumn(),
                    TextColumn("[progress.percentage]{task.percentage:>3.1f}%"),
                    TransferSpeedColumn(), TimeRemainingColumn(),
                    console=rich_console, transient=True
                ) as progress_bar:
                    task_id = progress_bar.add_task(f"  Uploading {file_path.name}", total=file_size)
                    s3_response_put = await client.put(url=str(prepare_response.presigned_url), content=file_data, headers=s3_headers)
                    progress_bar.update(task_id, completed=file_size, refresh=True)
            else:
                s3_response_put = await client.put(url=str(prepare_response.presigned_url), content=file_data, headers=s3_headers)

        s3_response_put.raise_for_status()
        s3_etag = s3_response_put.headers.get("ETag", "").strip('"')
    except (httpx.HTTPError, OSError) as e:
        raise UploadError(f"S3 upload failed for {file_path.name}. Reason: {e}")

    rich_console.print(f"  Confirming upload of {file_path.name} with backend...")
    # --- CHANGE: Reverted to build the full metadata payload ---
    complete_payload = CompleteSingleRequest(
        file_id=prepare_response.file_id,
        s3_etag=s3_etag,
        collection_name=prepare_payload.collection_name,
        file_name=prepare_payload.file_name,
        file_size_bytes=prepare_payload.file_size_bytes,
        checksum=prepare_payload.checksum,
        collection_path=prepare_payload.collection_path,
        content_type=prepare_payload.content_type
    )
    
    try:
        completion_response = await api_client.complete_single_upload(payload=complete_payload)
        rich_console.print(f"[green]  Successfully uploaded and confirmed {file_path.name}. Status: {completion_response.status}[/green]")
    except APIRequestError as e:
        logger.error(f"Backend confirmation failed for {file_path.name} (File ID: {prepare_response.file_id}): {e}")
        raise UploadError(f"Backend confirmation failed for {file_path.name}. Reason: {str(e)}")