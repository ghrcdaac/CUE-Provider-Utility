"""
Common utility functions used across the application.
Includes functions for checksum calculation, file operations, etc.
"""
import hashlib
import base64
import logging
from pathlib import Path
from typing import AsyncGenerator

import aiofiles
import puremagic 

from .exceptions import FileProcessingError

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE_BYTES = 1024 * 1024 * 32  # 32 MiB

async def calculate_sha256_checksum(file_path: Path, chunk_size: int = DEFAULT_CHUNK_SIZE_BYTES) -> str:
    """
    Calculates the SHA256 checksum of a file asynchronously and returns it Base64 encoded.
    """
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

def get_mime_type(file_path: Path) -> str:
    """
    Determines the MIME type of a file using puremagic.
    Falls back to common extensions or application/octet-stream.
    """
    try:
        mime = puremagic.from_file(str(file_path), mime=True)
        if mime:
            logger.debug(f"Determined MIME type for {file_path.name}: {mime} (using puremagic)")
            return mime
    except FileNotFoundError: # Should be caught by Path(exists=True) in Click earlier
        raise FileProcessingError(f"File not found for MIME type detection: {file_path}")
    except Exception as e: 
        logger.warning(f"puremagic failed to determine MIME type for {file_path.name}: {e}. Falling back.")

    ext_map = {
        '.zip': 'application/zip', '.txt': 'text/plain', '.json': 'application/json',
        '.xml': 'application/xml', '.csv': 'text/csv', '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif',
        '.pdf': 'application/pdf', '.tar': 'application/x-tar', '.gz': 'application/gzip',
        '.tgz': 'application/gzip', 
    }
    ext = file_path.suffix.lower()
    if ext in ext_map:
        logger.debug(f"Determined MIME type for {file_path.name}: {ext_map[ext]} (using fallback map)")
        return ext_map[ext]
    
    logger.warning(f"Could not determine specific MIME type for {file_path.name}. Defaulting to application/octet-stream.")
    return "application/octet-stream"


async def read_file_chunks(file_path: Path, chunk_size: int) -> AsyncGenerator[bytes, None]:
    """
    Asynchronously reads a file in chunks.
    """
    try:
        async with aiofiles.open(file_path, "rb") as f:
            while True:
                chunk = await f.read(chunk_size)
                if not chunk:
                    break
                yield chunk
    except OSError as e:
        logger.error(f"Error reading file {file_path} in chunks: {e}")
        raise FileProcessingError(f"Could not read file {file_path} in chunks.", original_exception=e)

def format_bytes(size_bytes: int) -> str:
    """Converts bytes to a human-readable string (KiB, MiB, GiB, TiB)."""
    if size_bytes == 0: return "0 B"
    if size_bytes < 1024: return f"{size_bytes} B"
    for unit in ['KiB', 'MiB', 'GiB', 'TiB']:
        size_bytes /= 1024.0
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
    return f"{size_bytes:.2f} PiB" 

DISALLOWED_EXTENSIONS = {'.dll', '.exe'} 

def is_file_type_disallowed(file_path: Path) -> bool:
    """Checks if the file extension is in the disallowed list."""
    return file_path.suffix.lower() in DISALLOWED_EXTENSIONS
