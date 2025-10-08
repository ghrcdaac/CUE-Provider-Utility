# cue_provider_utility/single_file_uploader.py

import asyncio
import logging
from pathlib import Path
from typing import Optional, Any

import aiofiles
import httpx
from rich.progress import Progress, BarColumn, TextColumn, TransferSpeedColumn, TimeRemainingColumn, SpinnerColumn

from .api_client import ApiClient
from .exceptions import APIRequestError, FileProcessingError, UploadError
from .logger_setup import rich_console
from .models import AppConfig, CompleteSingleRequest, PrepareSingleRequest
from .utils import calculate_sha256_checksum, get_mime_type

logger = logging.getLogger(__name__)


async def _perform_upload(
    file_path: Path,
    file_size: int,
    collection: str,
    target_sub_path: Optional[str],
    api_client: ApiClient,
    config: AppConfig,
    progress: Progress,
    task_id: Any,
):
    """Internal helper function to perform the upload steps with a guaranteed progress bar."""
    try:
        # Step 1: Checksum and MIME Type
        progress.update(task_id, description=f"Checksum {file_path.name}")
        checksum_sha256 = await calculate_sha256_checksum(file_path) # Removed progress from here as it's too fast to show

        progress.update(task_id, description=f"File type {file_path.name}")
        mime_type = await get_mime_type(file_path)

        # Step 2: Prepare the upload with the backend
        prepare_payload = PrepareSingleRequest(
            file_name=file_path.name,
            collection_name=collection,
            file_size_bytes=file_size,
            checksum=checksum_sha256,
            content_type=mime_type,
            collection_path=target_sub_path,
        )
        progress.update(task_id, description=f"Preparing {file_path.name}")
        prepare_response = await api_client.prepare_single_upload(prepare_payload)

     
        # We must read the entire file into memory to send the Content-Length header.
        async with aiofiles.open(file_path, 'rb') as f:
            file_data = await f.read()
        
        s3_headers = {'Content-Type': mime_type, 'Content-Length': str(file_size)}

        # Step 3: Upload the file data to the presigned URL
        progress.update(task_id, description=f"Uploading {file_path.name}")
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.put(
                url=str(prepare_response.presigned_url),
                content=file_data, # Send the full byte content
                headers=s3_headers # Send the required headers
            )
            response.raise_for_status()
            s3_etag = response.headers.get("ETag", "").strip('"')

        # Since the upload happens in one go, we complete the progress bar after it's done.
        progress.update(task_id, completed=file_size)

        # Step 4: Complete the upload with the backend
        progress.update(task_id, description=f"Completing {file_path.name}")
        complete_payload = CompleteSingleRequest(
            file_id=prepare_response.file_id,
            s3_etag=s3_etag,
            collection_name=prepare_payload.collection_name,
            file_name=prepare_payload.file_name,
            file_size_bytes=prepare_payload.file_size_bytes,
            checksum=prepare_payload.checksum,
            collection_path=prepare_payload.collection_path,
            content_type=prepare_payload.content_type,
        )
        await api_client.complete_single_upload(payload=complete_payload)

    except (httpx.HTTPError, OSError, FileProcessingError, APIRequestError) as e:
        # Re-raise as a standard UploadError for the folder processor to catch
        raise UploadError(f"Upload failed for {file_path.name}. Reason: {e}") from e


async def handle_single_file_upload(
    file_path: Path,
    file_size: int,
    collection: str,
    target_sub_path: Optional[str],
    api_client: ApiClient,
    config: AppConfig,
    progress: Optional[Progress] = None,
    task_id: Optional[Any] = None,
):
    """
    Handles the entire process of uploading a single file directly.
    """
    # If this is a standalone upload, create a temporary progress bar.
    if progress is None or task_id is None:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.1f}%"),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=rich_console,
            transient=True # Makes the bar disappear on completion
        ) as new_progress:
            new_task_id = new_progress.add_task(f"Uploading {file_path.name}", total=file_size)
            await _perform_upload(file_path, file_size, collection, target_sub_path, api_client, config, new_progress, new_task_id)
        
        rich_console.print(f"   [green]✓[/green] Successfully uploaded [bold cyan]{file_path.name}[/bold cyan].")
    else:
        # This is part of a folder upload; use the existing progress and task_id.
        await _perform_upload(file_path, file_size, collection, target_sub_path, api_client, config, progress, task_id)