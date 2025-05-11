"""
Core upload orchestrator.
Determines upload strategy (single, multipart, folder) and delegates processing.
Manages overall progress and error reporting for an upload operation.
"""
import asyncio
import logging
from pathlib import Path
from typing import Optional

from .models import AppConfig, GlobalArgs
from .exceptions import UploadError, FileProcessingError, APIRequestError, ConfigError
from .api_client import ApiClient
from .single_file_uploader import handle_single_file_upload
from .multipart_uploader import handle_multipart_upload
from .folder_processor import process_folder_upload
from .utils import get_file_size, is_file_type_disallowed
from .logger_setup import rich_console # For direct user feedback

logger = logging.getLogger(__name__)

async def process_upload(
    source_path: Path,
    collection: str,
    target_sub_path: Optional[str],
    auth_token: str,
    config: AppConfig,
    global_args: GlobalArgs,
    file_concurrency: int,
    part_concurrency: int,
    auto_approve: bool
):
    """
    Main entry point to process an upload request for a file or folder.
    """
    api_client = ApiClient(config=config, global_args=global_args, auth_token=auth_token)
    selected_env_url = str(getattr(config.environments, global_args.env_cli or config.default_env))


    try:
        if not source_path.exists():
            raise FileProcessingError(f"Source path does not exist: {source_path}")

        # --- Security Validation: Disallowed file types ---
        # For single file, check directly. For folders, this will be checked per file.
        if source_path.is_file() and is_file_type_disallowed(source_path):
            raise FileProcessingError(
                f"File type {source_path.suffix} is disallowed for upload. File: {source_path.name}"
            )

        if source_path.is_dir():
            logger.info(f"Processing folder upload for: {source_path}")
            await process_folder_upload(
                folder_path=source_path,
                collection=collection,
                target_sub_path=target_sub_path,
                api_client=api_client,
                config=config,
                global_args=global_args,
                file_concurrency=file_concurrency,
                part_concurrency=part_concurrency,
                auto_approve=auto_approve
            )
        elif source_path.is_file():
            logger.info(f"Processing single file upload for: {source_path.name}")
            file_size = get_file_size(source_path)
            
            # Multipart threshold from GiB to Bytes
            multipart_threshold_bytes = config.multipart_threshold_gb * (1024**3)

            if file_size > multipart_threshold_bytes:
                logger.info(f"File size {file_size} > threshold {multipart_threshold_bytes}. Using multipart upload.")
                await handle_multipart_upload(
                    file_path=source_path,
                    file_size=file_size,
                    collection=collection,
                    target_sub_path=target_sub_path,
                    api_client=api_client,
                    config=config,
                    global_args=global_args,
                    part_concurrency=part_concurrency
                )
            else:
                logger.info(f"File size {file_size} <= threshold {multipart_threshold_bytes}. Using single file upload.")
                await handle_single_file_upload(
                    file_path=source_path,
                    file_size=file_size,
                    collection=collection,
                    target_sub_path=target_sub_path,
                    api_client=api_client,
                    config=config,
                    global_args=global_args
                )
        else:
            raise FileProcessingError(f"Source path is not a file or directory: {source_path}")

    except (UploadError, FileProcessingError, APIRequestError, ConfigError) as e:
        logger.error(f"Upload processing failed: {e}", exc_info=global_args.verbose_level >=2)
        rich_console.print(f"[bold red]Error during upload: {e}[/bold red]")
        # Re-raise or handle as appropriate for main CLI error reporting
        raise
    except Exception as e:
        logger.critical(f"An unexpected critical error occurred in upload processing: {e}", exc_info=True)
        rich_console.print(f"[bold red]Critical unexpected error: {e}[/bold red]")
        raise UploadError(f"An unexpected critical error occurred: {e}", original_exception=e)
    finally:
        if api_client:
            await api_client.close()
        logger.info("Upload processing finished.")

