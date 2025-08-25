# In cue_provider_utility/uploader.py
# (This is the complete updated file content)

"""
Core upload orchestrator.
Determines upload strategy (single, multipart, folder) and delegates processing.
Manages overall progress and error reporting for an upload operation.
"""
import logging
from pathlib import Path
from typing import Optional

from .models import AppConfig, GlobalArgs
from .exceptions import UploadError, FileProcessingError, APIRequestError, ConfigError
from .api_client import ApiClient
from .single_file_uploader import handle_single_file_upload
from .multipart_uploader import handle_multipart_upload
from .folder_processor import process_folder_upload
from .utils import get_file_size, validate_file_type, format_bytes 
from .logger_setup import rich_console 

logger = logging.getLogger(__name__)

async def process_upload(
    source_path: Path,
    collection: str,
    target_sub_path: Optional[str],
    api_key: str, # Variable name changed
    config: AppConfig,
    global_args: GlobalArgs,
    file_concurrency: int,
    part_concurrency: int,
    auto_approve: bool
):
    """
    Main entry point to process an upload request for a file or folder.
    """
    selected_env_name = global_args.env_cli or config.default_env
    try:
        if selected_env_name not in config.environments.model_fields:
            raise ConfigError(f"Environment '{selected_env_name}' not defined in config.")
    except AttributeError: 
        raise ConfigError(f"Environment URL for '{selected_env_name}' not found.")

    # Pass the api_key to the ApiClient
    api_client = ApiClient(config=config, global_args=global_args, api_key=api_key)
    
    try:
        if not source_path.exists():
            raise FileProcessingError(f"Source path does not exist: {source_path}")
        
        if source_path.is_file():
            await validate_file_type(source_path, config)

        if source_path.is_dir():
            logger.info(f"Processing folder upload for: {source_path}")
            await process_folder_upload(
                folder_path=source_path, collection=collection, 
                target_sub_path=target_sub_path,
                api_client=api_client, config=config, global_args=global_args,
                file_concurrency=file_concurrency, part_concurrency=part_concurrency,
                auto_approve=auto_approve
            )
        elif source_path.is_file():
            logger.info(f"Processing file upload for: {source_path.name}")
            file_size = get_file_size(source_path)
            multipart_threshold_bytes = config.multipart_threshold_gb * (1024**3)
            
            if file_size > multipart_threshold_bytes:
                await handle_multipart_upload(
                    file_path=source_path, file_size=file_size, collection=collection,
                    target_sub_path=target_sub_path, api_client=api_client, config=config,
                    part_concurrency=part_concurrency
                )
            else:
                await handle_single_file_upload( 
                    file_path=source_path, file_size=file_size, collection=collection,
                    target_sub_path=target_sub_path, api_client=api_client, config=config
                )
        else:
            raise FileProcessingError(f"Source path is not a file or directory: {source_path}")

    except (UploadError, FileProcessingError, APIRequestError, ConfigError) as e:
        logger.error(f"Upload processing failed: {e}", exc_info=global_args.verbose_level >=2)
        raise
    except Exception as e:
        logger.critical(f"An unexpected critical error occurred in upload processing: {e}", exc_info=True)
        raise UploadError(f"An unexpected critical error occurred: {e}", original_exception=e)
    finally:
        if 'api_client' in locals() and api_client: 
            await api_client.close()
        logger.debug("process_upload function finished.")