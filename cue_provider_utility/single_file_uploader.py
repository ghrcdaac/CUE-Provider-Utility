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

from .models import AppConfig, GlobalArgs, InitiateUploadRequest, ConfirmSingleUploadRequest, InitiateUploadResponse
from .exceptions import UploadError, FileProcessingError, APIRequestError
from .api_client import ApiClient
from .utils import calculate_sha256_checksum, get_mime_type, format_bytes
from .logger_setup import rich_console
from rich.progress import Progress, BarColumn, TextColumn, TransferSpeedColumn, TimeRemainingColumn
import aiofiles 

logger = logging.getLogger(__name__)

async def handle_single_file_upload( # Ensure this function name is exact
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
        logger.info(f"SHA256 for {file_path.name}: {checksum_sha256}")
    except FileProcessingError as e:
        raise UploadError(f"Checksum calculation failed for {file_path.name}.", original_exception=e)

    mime_type = get_mime_type(file_path) 
    logger.info(f"MIME type for {file_path.name}: {mime_type}")

    initiate_payload = InitiateUploadRequest(
        file_name=file_path.name,
        collection=collection,
        size=file_size,
        checksum=checksum_sha256,
        file_type=mime_type,
        collection_path=target_sub_path
    )
    
    presigned_info: Optional[InitiateUploadResponse] = None # Explicit type
    for attempt in range(config.retry_attempts + 1):
        try:
            rich_console.print(f"  Requesting upload URL for {file_path.name} (attempt {attempt + 1}/{config.retry_attempts + 1})...")
            presigned_info = await api_client.get_presigned_url_single(initiate_payload)
            logger.info(f"Received presigned URL info for {file_path.name}. S3 Key: {presigned_info.s3_key}")
            break 
        except APIRequestError as e:
            logger.warning(f"Attempt {attempt + 1} to get presigned URL for {file_path.name} failed: {e}")
            if attempt >= config.retry_attempts:
                raise UploadError(f"Failed to get presigned URL for {file_path.name} after {config.retry_attempts + 1} attempts.", original_exception=e)
            await asyncio.sleep(2**attempt) 
    
    if presigned_info is None: 
         raise UploadError(f"Failed to obtain presigned URL for {file_path.name} (logic safeguard).")

    # Upload to S3
    if presigned_info.fields is not None: # Presigned POST
        rich_console.print(f"  Uploading {file_path.name} to S3 (Presigned POST)...")
        try:
            s3_response = await api_client.upload_to_s3_presigned_post(
                url=str(presigned_info.url), 
                fields=presigned_info.fields,
                file_path=file_path,
                file_name=file_path.name, 
                content_type=mime_type
            )
            if s3_response.status_code == 204: 
                s3_etag = s3_response.headers.get("ETag", "").strip('"')
                logger.info(f"Successfully uploaded {file_path.name} to S3 (POST). ETag: {s3_etag}")
            else:
                raise UploadError(f"S3 upload (POST) failed for {file_path.name} with status {s3_response.status_code}.")
        except APIRequestError as e: 
            raise UploadError(f"S3 upload (POST) failed for {file_path.name}.", original_exception=e)
    else: # Assume Presigned PUT
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

            if s3_response_put.status_code == 200: 
                s3_etag = s3_response_put.headers.get("ETag", "").strip('"')
                logger.info(f"Successfully uploaded {file_path.name} to S3 (PUT). ETag: {s3_etag}")
            else:
                raise UploadError(f"S3 upload (PUT) failed for {file_path.name} with status {s3_response_put.status_code}.")
        except APIRequestError as e:
            raise UploadError(f"S3 upload (PUT) failed for {file_path.name}.", original_exception=e)
        except OSError as e:
            raise FileProcessingError(f"Could not read file {file_path.name} for upload.", original_exception=e)

    # Confirm successful S3 upload with the backend
    confirm_payload = ConfirmSingleUploadRequest(
        s3_key=presigned_info.s3_key,
        file_name=file_path.name,
        collection=collection, 
        size_bytes=file_size,
        checksum=checksum_sha256,
        file_type=mime_type,
        collection_path=target_sub_path,
        s3_etag=s3_etag if s3_etag else None
    )
    try:
        rich_console.print(f"  Confirming upload of {file_path.name} with backend...")
        await api_client.confirm_single_upload(confirm_payload)
        logger.info(f"Backend confirmation successful for {file_path.name} (S3 Key: {presigned_info.s3_key}).")
        rich_console.print(f"[green]  Successfully uploaded and confirmed {file_path.name}.[/green]")
    except APIRequestError as e:
        logger.error(f"Backend confirmation failed for {file_path.name} (S3 Key: {presigned_info.s3_key}): {e}")
        raise UploadError(
            f"File {file_path.name} uploaded to S3, but backend confirmation failed: {e}. "
            f"S3 Key: {presigned_info.s3_key}. Please report this issue.",
            original_exception=e
        )

    logger.info(f"Single file upload process completed for {file_path.name}.")

