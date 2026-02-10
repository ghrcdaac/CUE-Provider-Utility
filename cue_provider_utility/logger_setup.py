"""
Configures logging for the application.
Sets up console and file logging with appropriate levels and formatting.
"""
import logging
import sys
from pathlib import Path
import asyncio
import time 
from typing import Optional

import aiofiles
from rich.logging import RichHandler 
from rich.console import Console
from rich.text import Text

from .config_manager import DEFAULT_CONFIG_DIR

DEFAULT_LOG_FILENAME = "cue-upload.log"
CONSOLE_FORMAT = "%(message)s"
FILE_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

rich_console = Console(stderr=True)
logger = logging.getLogger(__name__)

def get_log_file_path(config_path_override: Optional[Path] = None) -> Path:
    """
    Determines the log file path. 
    Priority: 
    1. Path defined in config.toml (via get_config)
    2. Default system path (via AppConfig defaults)
    """
    try:
        # Import inside function to avoid circular import issues
        from .config_manager import get_config
        
        # Try to load the actual user configuration file
        cfg = get_config(config_path_override)
        log_dir = cfg.log_file_directory
        
    except Exception:
        # If config file is corrupt or missing, use  original logic
        from .models import AppConfig
        log_dir = AppConfig().log_file_directory

    # Ensure the directory exists (This part stays exactly the same!)
    log_dir.mkdir(parents=True, exist_ok=True)
    
    return (log_dir / DEFAULT_LOG_FILENAME).resolve()

def setup_logging(
    log_path: Optional[Path] = None,
    file_log_level: str = "INFO", 
    console_log_level: str = "INFO",
    disable_file_logging: bool = False
) -> None:
    effective_log_path = (log_path or get_log_file_path()).resolve()
    effective_log_path.parent.mkdir(parents=True, exist_ok=True)

    numeric_file_level = getattr(logging, file_log_level.upper(), logging.INFO)
    numeric_console_level = getattr(logging, console_log_level.upper(), logging.INFO)
    lowest_level = min(numeric_file_level, numeric_console_level)
    
    root_logger = logging.getLogger()
    root_logger.setLevel(lowest_level)
    
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    rich_handler = RichHandler(
        console=rich_console, show_time=False, show_level=True, show_path=False,
        markup=True, rich_tracebacks=True, tracebacks_show_locals=True,
    )
    rich_handler.setLevel(numeric_console_level)
    root_logger.addHandler(rich_handler)

    if not disable_file_logging:
        try:
            file_handler = logging.FileHandler(effective_log_path, encoding='utf-8')
            file_handler.setLevel(numeric_file_level)
            file_formatter = logging.Formatter(FILE_FORMAT, datefmt=DATE_FORMAT)
            file_handler.setFormatter(file_formatter)
            root_logger.addHandler(file_handler)
        except Exception as e:
            root_logger.error(f"Failed to configure file logging at {effective_log_path}: {e}")

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logger.debug(f"Logging setup complete. Console: {console_log_level}, File: {file_log_level} at {effective_log_path}")

async def view_log_file(log_file_path: Path, lines: Optional[int], follow: bool, raw: bool) -> None:
    """Displays log file content to the console."""
    if not log_file_path.exists():
        rich_console.print(f"[bold red]Error: Log file not found at {log_file_path}[/bold red]")
        return

    try:
        async with aiofiles.open(log_file_path, "r", encoding="utf-8") as f:
            if lines:
                log_lines = await f.readlines()
                for line in log_lines[-lines:]:
                    rich_console.print(Text.from_ansi(line.rstrip()) if raw else line.rstrip())
            
            elif follow:
                rich_console.print(f"[cyan]Following log file: {log_file_path} (Ctrl+C to stop)[/cyan]")
                await f.seek(0, 2)
                while True:
                    # The try/except for asyncio.CancelledError handles the interrupt
                    try:
                        line = await f.readline()
                        if not line:
                            await asyncio.sleep(0.25) # Yield control
                            continue
                        rich_console.print(Text.from_ansi(line.rstrip()) if raw else line.rstrip())
                    except asyncio.CancelledError:
                        # This is the expected exception when the main task is cancelled by Ctrl+C
                        break # Break the while loop
            else:
                async for line in f:
                    rich_console.print(Text.from_ansi(line.rstrip()) if raw else line.rstrip())
    except Exception as e:
        # Avoid printing an error on a clean exit
        if not isinstance(e, asyncio.CancelledError):
            rich_console.print(f"[bold red]Error reading log file: {e}[/bold red]")

