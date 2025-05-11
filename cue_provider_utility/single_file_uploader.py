"""
Handles the process for uploading a single file (non-multipart).
Interacts with the ApiClient to get a presigned URL and then uploads the file to S3.
"""
import logging
from pathlib import Path
from typing import Optional # <<< IMPORT ADDED HERE
import asyncio 
import aiofiles

from .models import AppConfig, GlobalArgs, InitiateUploadRequest
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
    target_sub_path: Optional[str], # <<< Optional used here
    api_client: ApiClient,
    config: AppConfig,
    global_args: GlobalArgs
):
    """
    Orchestrates the upload of a single small file.
    """
    rich_console.print(f"[info]Preparing single file upload for: [bold cyan]{file_path.name}[/bold cyan] ({format_bytes(file_size)})")

    try:
        rich_console.print(f"  Calculating SHA256 checksum for {file_path.name}...")
        checksum_sha256 = await calculate_sha256_checksum(file_path)
        logger.info(f"SHA256 for {file_path.name}: {checksum_sha256}")
    except FileProcessingError as e:
        logger.error(f"Failed to calculate checksum for {file_path.name}: {e}")
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
    
    presigned_info = None # Define outside loop for clarity
    for attempt in range(config.retry_attempts + 1):
        try:
            rich_console.print(f"  Requesting upload URL for {file_path.name} (attempt {attempt + 1}/{config.retry_attempts + 1})...")
            presigned_info = await api_client.get_presigned_url_single(initiate_payload)
            logger.info(f"Received presigned URL information for {file_path.name}.")
            break 
        except APIRequestError as e:
            logger.warning(f"Attempt {attempt + 1} to get presigned URL for {file_path.name} failed: {e}")
            if attempt >= config.retry_attempts:
                raise UploadError(f"Failed to get presigned URL for {file_path.name} after {config.retry_attempts + 1} attempts.", original_exception=e)
            await asyncio.sleep(2**attempt) 
    else: 
        # This else block for a for loop executes if the loop completed normally (no break)
        # which implies all retry attempts failed to get a presigned_info.
        # However, the raise UploadError inside the loop should prevent this.
        # Adding a safeguard:
        if presigned_info is None:
             raise UploadError(f"Failed to obtain presigned URL for {file_path.name} after all retries (logic safeguard).")


    if presigned_info.fields is not None: 
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
                logger.info(f"Successfully uploaded {file_path.name} to S3 (POST). Status: {s3_response.status_code}")
                rich_console.print(f"[green]  Successfully uploaded {file_path.name}.[/green]")
            else:
                logger.error(f"S3 POST upload for {file_path.name} returned unexpected status: {s3_response.status_code}. Response: {s3_response.text[:200]}")
                raise UploadError(f"S3 upload (POST) failed for {file_path.name} with status {s3_response.status_code}.")

        except APIRequestError as e: 
            logger.error(f"S3 POST upload failed for {file_path.name}: {e}")
            raise UploadError(f"S3 upload (POST) failed for {file_path.name}.", original_exception=e)

    else: 
        rich_console.print(f"  Uploading {file_path.name} to S3 (Presigned PUT)...")
        try:
            # Read file content. For large "single" files (just under multipart threshold),
            # this could be memory intensive. Consider streaming if this becomes an issue.
            async with aiofiles.open(file_path, 'rb') as f:
                file_data = await f.read()
            
            with Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.1f}%"),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=rich_console,
                transient=True # Clears progress on exit
            ) as progress_bar: # Renamed from 'progress' to avoid conflict
                task_id = progress_bar.add_task(f"  Uploading {file_path.name}", total=file_size)

                # Simulate progress for PUT as it's a single chunk here
                # In a real streaming PUT, you'd update progress as chunks are sent.
                s3_response_put = await api_client.upload_part_to_s3_presigned_put( 
                    url=str(presigned_info.url),
                    part_data=file_data,
                    content_length=file_size
                )
                progress_bar.update(task_id, completed=file_size, refresh=True)

            if s3_response_put.status_code == 200: 
                logger.info(f"Successfully uploaded {file_path.name} to S3 (PUT). Status: {s3_response_put.status_code}")
                rich_console.print(f"[green]  Successfully uploaded {file_path.name}.[/green]")
            else:
                logger.error(f"S3 PUT upload for {file_path.name} returned unexpected status: {s3_response_put.status_code}. Response: {s3_response_put.text[:200]}")
                raise UploadError(f"S3 upload (PUT) failed for {file_path.name} with status {s3_response_put.status_code}.")
        
        except APIRequestError as e:
            logger.error(f"S3 PUT upload failed for {file_path.name}: {e}")
            raise UploadError(f"S3 upload (PUT) failed for {file_path.name}.", original_exception=e)
        except OSError as e:
            logger.error(f"Failed to read file {file_path.name} for S3 PUT: {e}")
            raise FileProcessingError(f"Could not read file {file_path.name} for upload.", original_exception=e)

    logger.info(f"Single file upload process completed for {file_path.name}.")
