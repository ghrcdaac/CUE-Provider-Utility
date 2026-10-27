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
    cached_checksum: Optional[str] = None,
    cached_content_type: Optional[str] = None,
) -> tuple[str, str]:
    """Internal helper function to perform the upload steps with a guaranteed progress bar. Returns (checksum, mime_type)."""
    try:
        # Step 1: Checksum and MIME Type
        if cached_checksum:
            checksum_sha256 = cached_checksum
        else:
            progress.update(task_id, description=f"Checksum {file_path.name}")
            checksum_sha256 = await calculate_sha256_checksum(file_path)

        if cached_content_type:
            mime_type = cached_content_type
        else:
            progress.update(task_id, description=f"File type {file_path.name}")
            mime_type = await get_mime_type(file_path)

        try:
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

            s3_headers = {'Content-Type': mime_type, 'Content-Length': str(file_size)}

            # Step 3: Upload the file data to the presigned URL
            progress.reset(task_id, total=file_size, description=f"Uploading {file_path.name}")
            async with httpx.AsyncClient(timeout=None) as client:
                response = await client.put(
                    url=str(prepare_response.presigned_url),
                    content=api_client.read_in_chunks(file_path, progress=progress, task_id=task_id),
                    headers=s3_headers
                )
                response.raise_for_status()
                s3_etag = response.headers.get("ETag", "").strip('"')

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
            return checksum_sha256, mime_type
        except Exception as e:
            if not isinstance(e, UploadError):
                err = UploadError(f"Upload failed for {file_path.name}. Reason: {e}", original_exception=e)
            else:
                err = e
            setattr(err, "checksum", checksum_sha256)
            setattr(err, "content_type", mime_type)
            raise err

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
    cached_checksum: Optional[str] = None,
    cached_content_type: Optional[str] = None,
) -> tuple[str, str]:
    """
    Handles the entire process of uploading a single file directly. Returns (checksum, content_type).
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
            result = await _perform_upload(
                file_path, file_size, collection, target_sub_path, api_client, config,
                new_progress, new_task_id, cached_checksum, cached_content_type
            )
        
        rich_console.print(f"   [green]✓[/green] Successfully uploaded [bold cyan]{file_path.name}[/bold cyan].")
        return result
    else:
        # This is part of a folder upload; use the existing progress and task_id.
        return await _perform_upload(
            file_path, file_size, collection, target_sub_path, api_client, config,
            progress, task_id, cached_checksum, cached_content_type
        )