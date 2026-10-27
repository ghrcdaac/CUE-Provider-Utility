# cue_provider_utility/failed_uploads_handler.py

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from .models import FailedUploadEntry, FailedUploadsContainer, AppConfig
from .config_manager import get_config

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()


def get_default_failed_log_path(config_path_override: Optional[Path] = None) -> Path:
    """Returns the configured failed uploads log path."""
    try:
        config = get_config(config_path_override)
        return config.failed_log_path
    except Exception:
        return Path.home() / ".cue-upload" / "failed_uploads.json"


async def load_failed_uploads_container(log_path: Path) -> FailedUploadsContainer:
    """Loads the failed uploads container from JSON file, creating default if not found or empty."""
    if not log_path.exists():
        return FailedUploadsContainer()
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            container = FailedUploadsContainer(**data)
        elif isinstance(data, list):
            # Backward compatibility if JSON is a raw list
            entries = [FailedUploadEntry(**item) for item in data if isinstance(item, dict)]
            container = FailedUploadsContainer(failed_uploads=entries)
        else:
            container = FailedUploadsContainer()

        # Sanitize entries: ensure entry_id and timestamp exist
        for entry in container.failed_uploads:
            if not entry.entry_id:
                entry.entry_id = str(uuid4())
            if not entry.timestamp:
                entry.timestamp = datetime.now(timezone.utc).isoformat()
        return container
    except Exception as e:
        logger.error(f"Error reading failed uploads file at {log_path}: {e}")
        return FailedUploadsContainer()


async def save_failed_uploads_container(container: FailedUploadsContainer, log_path: Path) -> None:
    """Saves the failed uploads container to JSON file atomically."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        container_dict = container.model_dump(mode='json', exclude_none=False)
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(container_dict, f, indent=2)
        logger.debug(f"Saved failed uploads log to {log_path}")
    except Exception as e:
        logger.error(f"Failed to save failed uploads log to {log_path}: {e}")


class FailedUploadsManager:
    """Thread-safe manager for failed upload logs."""

    @staticmethod
    async def add_failed_entry(entry: FailedUploadEntry, log_path: Path) -> FailedUploadEntry:
        """Appends or updates a failed upload entry in the log file."""
        async with _lock:
            if not entry.entry_id:
                entry.entry_id = str(uuid4())
            if not entry.timestamp:
                entry.timestamp = datetime.now(timezone.utc).isoformat()

            container = await load_failed_uploads_container(log_path)
            
            # Remove any pre-existing entry for the exact same file path and collection if present
            container.failed_uploads = [
                e for e in container.failed_uploads
                if not (e.file_path.resolve() == entry.file_path.resolve() and e.collection == entry.collection and e.target_sub_path == entry.target_sub_path)
            ]
            container.failed_uploads.append(entry)
            await save_failed_uploads_container(container, log_path)
            return entry

    @staticmethod
    async def remove_failed_entry(entry_id: str, log_path: Path) -> bool:
        """Removes a failed upload entry by its entry_id."""
        async with _lock:
            container = await load_failed_uploads_container(log_path)
            initial_count = len(container.failed_uploads)
            container.failed_uploads = [e for e in container.failed_uploads if e.entry_id != entry_id]
            removed = len(container.failed_uploads) < initial_count
            if removed:
                await save_failed_uploads_container(container, log_path)
            return removed

    @staticmethod
    async def get_failed_entries(log_path: Path, session_id: Optional[str] = None) -> List[FailedUploadEntry]:
        """Retrieves failed upload entries, optionally filtered by session_id."""
        async with _lock:
            container = await load_failed_uploads_container(log_path)
            if session_id:
                return [e for e in container.failed_uploads if e.session_id == session_id]
            return list(container.failed_uploads)

    @staticmethod
    async def clear_failed_entries(log_path: Path) -> None:
        """Clears all entries in the failed uploads log."""
        async with _lock:
            await save_failed_uploads_container(FailedUploadsContainer(), log_path)
