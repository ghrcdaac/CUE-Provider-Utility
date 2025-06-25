"""
Handles the process for uploading a single file (non-multipart).
1. Gets a presigned URL from the backend (which includes an s3_key).
2. Uploads the file to S3 using the presigned URL.
3. Confirms the successful S3 upload with the backend.
"""
import logging
from pathlib import Path
from typing import Optional
import asyncio

import aiofiles 

from .models import AppConfig, GlobalArgs, InitiateUploadRequest, ConfirmSingleUploadRequest, InitiateUploadResponse
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
    global_args: GlobalArgs
):
    rich_console.print(f"[info]Preparing single file upload for: [bold cyan]{file_path.name}[/bold cyan] ({format_bytes(file_size)})")

    s3_etag: Optional[str] = None 

    try:
        rich_console.print(f"  Calculating SHA256 checksum for {file_path.name}...")
        checksum_sha256 = await calculate_sha256_checksum(file_path)
    except FileProcessingError as e:
        raise UploadError(f"Checksum calculation failed for {file_path.name}: {e}", original_exception=e)

    mime_type = await get_mime_type(file_path)

    initiate_payload = InitiateUploadRequest(
        file_name=file_path.name,
        collection=collection,
        size=file_size,
        checksum=checksum_sha256,
        file_type=mime_type,
        collection_path=target_sub_path
    )
    
    presigned_info: Optional[InitiateUploadResponse] = None
    for attempt in range(config.retry_attempts + 1):
        try:
            attempt_msg = f" (attempt {attempt + 1}/{config.retry_attempts + 1})" if attempt > 0 else ""
            rich_console.print(f"  Requesting upload URL for {file_path.name}{attempt_msg}...")
            presigned_info = await api_client.get_presigned_url_single(initiate_payload)
            break 
        except APIRequestError as e:
            logger.warning(f"Attempt {attempt + 1} to get presigned URL failed: {e}")
            if attempt >= config.retry_attempts:
                # FIX: Propagate the specific error message from the APIRequestError
                raise UploadError(f"Failed to get presigned URL for {file_path.name}. Reason: {e}")
            await asyncio.sleep(2**attempt)
    
    if presigned_info is None: 
        raise UploadError(f"Could not obtain presigned URL for {file_path.name} after retries.")

    if presigned_info.fields is not None: 
        rich_console.print(f"  Uploading {file_path.name} to S3 (Presigned POST)...")
        try:
            s3_response = await api_client.upload_to_s3_presigned_post(
                url=str(presigned_info.url), fields=presigned_info.fields,
                file_path=file_path, file_name=file_path.name, content_type=mime_type
            )
            s3_etag = s3_response.headers.get("ETag", "").strip('"')
        except APIRequestError as e: 
            raise UploadError(f"S3 upload failed for {file_path.name}. Reason: {e}")
    else: 
        rich_console.print(f"  Uploading {file_path.name} to S3 (Presigned PUT)...")
        try:
            async with aiofiles.open(file_path, 'rb') as f:
                file_data = await f.read()
            
            with Progress(
                TextColumn("[progress.description]{task.description}"), BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.1f}%"),
                TransferSpeedColumn(), TimeRemainingColumn(),
                console=rich_console, transient=True
            ) as progress_bar:
                task_id = progress_bar.add_task(f"  Uploading {file_path.name}", total=file_size)
                s3_response_put = await api_client.upload_part_to_s3_presigned_put( 
                    url=str(presigned_info.url), part_data=file_data, content_length=file_size
                )
                progress_bar.update(task_id, completed=file_size, refresh=True)

            s3_etag = s3_response_put.headers.get("ETag", "").strip('"')
        except (APIRequestError, OSError) as e:
            raise UploadError(f"S3 upload failed for {file_path.name}. Reason: {e}")

    confirm_payload = ConfirmSingleUploadRequest(
        s3_key=presigned_info.s3_key, file_name=file_path.name, collection=collection, 
        size_bytes=file_size, checksum=checksum_sha256, file_type=mime_type,
        collection_path=target_sub_path, s3_etag=s3_etag if s3_etag else None
    )
    try:
        rich_console.print(f"  Confirming upload of {file_path.name} with backend...")
        await api_client.confirm_single_upload(confirm_payload)
        rich_console.print(f"[green]  Successfully uploaded and confirmed {file_path.name}.[/green]")
    except APIRequestError as e:
        logger.error(f"Backend confirmation failed for {file_path.name} (S3 Key: {presigned_info.s3_key}): {e}")
        # FIX: Propagate the specific error message
        raise UploadError(f"Backend confirmation failed. Reason: {e}")
