"""
Common utility functions used across the application.
Includes functions for checksum calculation, file operations, etc.
"""
import hashlib
import base64
import logging
from pathlib import Path
from typing import AsyncGenerator
import asyncio

import aiofiles
import puremagic 

from .exceptions import FileProcessingError
from .models import AppConfig

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE_BYTES = 1024 * 1024 * 32  # 32 MiB
# A small set of executable extensions for the secondary filename check
SUSPICIOUS_EXTENSIONS = {'.exe', '.dll', '.so', '.dmg', '.sh', '.bat'} 

async def calculate_sha256_checksum(file_path: Path, chunk_size: int = DEFAULT_CHUNK_SIZE_BYTES) -> str:
    """Calculates the SHA256 checksum of a file asynchronously and returns it Base64 encoded."""
    sha256_hash = hashlib.sha256()
    try:
        async with aiofiles.open(file_path, "rb") as f:
            while True:
                chunk = await f.read(chunk_size)
                if not chunk:
                    break
                sha256_hash.update(chunk)
        return base64.b64encode(sha256_hash.digest()).decode('utf-8')
    except OSError as e:
        logger.error(f"Error reading file {file_path} for SHA256 checksum: {e}")
        raise FileProcessingError(f"Could not read file {file_path} to calculate checksum.", original_exception=e)

async def calculate_sha256_checksum_for_bytes(data: bytes) -> str:
    """Calculates SHA256 checksum for a bytes object, returns Base64 encoded."""
    sha256_hash = hashlib.sha256()
    sha256_hash.update(data)
    return base64.b64encode(sha256_hash.digest()).decode('utf-8')


def get_file_size(file_path: Path) -> int:
    """Gets the size of a file in bytes."""
    try:
        return file_path.stat().st_size
    except OSError as e:
        logger.error(f"Error getting size of file {file_path}: {e}")
        raise FileProcessingError(f"Could not get size of file {file_path}.", original_exception=e)

async def get_mime_type(file_path: Path) -> str:
    """
    Determines the MIME type of a file using puremagic asynchronously.
    Falls back to common extensions or application/octet-stream.
    """
    try:
        # Run synchronous puremagic in a separate thread to avoid blocking asyncio event loop
        mime = await asyncio.to_thread(puremagic.from_file, str(file_path), mime=True)
        if mime:
            logger.debug(f"Determined MIME type for {file_path.name}: {mime} (using puremagic)")
            return mime
    except Exception as e: 
        logger.warning(f"puremagic failed for {file_path.name}: {e}. Falling back to extension map.")

    # Add fallback for common scientific data formats
    ext_map = {
        '.zip': 'application/zip', '.txt': 'text/plain', '.json': 'application/json',
        '.xml': 'application/xml', '.csv': 'text/csv', '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif',
        '.pdf': 'application/pdf', '.tar': 'application/x-tar', '.gz': 'application/gzip',
        '.tgz': 'application/gzip', '.nc': 'application/x-netcdf', '.hdf': 'application/x-hdf',
        '.h4': 'application/x-hdf', '.h5': 'application/x-hdf'
    }
    ext = file_path.suffix.lower()
    if ext in ext_map:
        logger.debug(f"Determined MIME type for {file_path.name}: {ext_map[ext]} (using fallback map)")
        return ext_map[ext]
    
    logger.warning(f"Could not determine MIME type for {file_path.name}. Defaulting to application/octet-stream.")
    return "application/octet-stream"

async def validate_file_type(file_path: Path, config: AppConfig) -> None:
    """
    Performs robust file type validation using MIME types and filename checks.
    Raises FileProcessingError if validation fails.
    """
    logger.debug(f"Validating file type for: {file_path.name}")
    
    # 1. Primary check: Use MIME type from file content ("magic numbers")
    mime_type = await get_mime_type(file_path)
    
    # Check against deny list (highest priority)
    if mime_type in config.denied_mime_types:
        raise FileProcessingError(f"File '{file_path.name}' has a disallowed MIME type: '{mime_type}'. Upload rejected.")

    # If an allow list is configured, enforce it
    if config.allowed_mime_types and mime_type not in config.allowed_mime_types:
        raise FileProcessingError(f"File '{file_path.name}' has MIME type '{mime_type}', which is not in the configured allowed list.")

    # 2. Secondary check: Look for suspicious extensions within the filename
    filename_lower = file_path.name.lower()
    parts = filename_lower.split('.')
    # Check for suspicious extensions in parts before the last one (e.g., "virus.exe.pdf")
    if len(parts) > 2:
        for part in parts[:-1]:
            # Construct the extension with a dot to match the set
            inner_ext = f".{part}"
            if inner_ext in SUSPICIOUS_EXTENSIONS:
                raise FileProcessingError(f"File '{file_path.name}' contains a suspicious inner extension '{inner_ext}'. Upload rejected for security.")
    
    logger.debug(f"File type validation passed for {file_path.name} (MIME: {mime_type})")


def format_bytes(size_bytes: int) -> str:
    """Converts bytes to a human-readable string (KiB, MiB, GiB, TiB)."""
    if size_bytes == 0: return "0 B"
    if size_bytes < 1024: return f"{size_bytes} B"
    for unit in ['KiB', 'MiB', 'GiB', 'TiB']:
        size_bytes /= 1024.0
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
    return f"{size_bytes:.2f} PiB" 

