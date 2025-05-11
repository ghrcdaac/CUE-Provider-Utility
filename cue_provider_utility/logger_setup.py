"""
Configures logging for the application.
Sets up console and file logging with appropriate levels and formatting.
"""
import logging
import sys
from pathlib import Path
import asyncio
import time # For the --follow functionality

from rich.logging import RichHandler # For prettier console logs
from rich.console import Console
from rich.text import Text

from .config_manager import DEFAULT_CONFIG_DIR # To get default log dir base

DEFAULT_LOG_FILENAME = "cue-upload.log"
CONSOLE_FORMAT = "%(message)s" # RichHandler handles most formatting
FILE_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Global console object for rich printing, can be imported elsewhere
rich_console = Console(stderr=True) # Direct errors and logs to stderr by default

def get_log_file_path(config_path_override: Optional[Path] = None) -> Path:
    """
    Determines the log file path.
    Note: This doesn't use get_config() to avoid circular dependencies during initial setup
    if get_config itself logs. It relies on the default base directory.
    """
    # This is a simplified way to get the log dir from AppConfig defaults
    # In a real scenario, if log_file_directory could be set by an env var before config is loaded,
    # that would need more complex handling. For now, assume default from model.
    from .models import AppConfig # Local import to access default
    log_dir = AppConfig().log_file_directory # Gets default value

    if config_path_override: # If config path is overridden, log dir might be relative or absolute
        # This logic might need refinement based on how log_file_override is handled in main.py
        # For now, assume log_file_override in main.py handles full path construction.
        # This function primarily provides the *default* log file path.
        pass # Handled by main.py's log_file_override logic

    log_dir.mkdir(parents=True, exist_ok=True)
    return (log_dir / DEFAULT_LOG_FILENAME).resolve()


def setup_logging(
    log_path: Optional[Path] = None,
    file_log_level: str = "INFO", # From config, typically
    console_log_level: str = "INFO", # From CLI verbosity
    disable_file_logging: bool = False
) -> None:
    """
    Configures root logger with console and file handlers.
    """
    # Ensure log_path is absolute and directory exists
    effective_log_path = (log_path or get_log_file_path()).resolve()
    effective_log_path.parent.mkdir(parents=True, exist_ok=True)

    # Get numeric log levels
    numeric_file_level = getattr(logging, file_log_level.upper(), logging.INFO)
    numeric_console_level = getattr(logging, console_log_level.upper(), logging.INFO)

    # Configure root logger
    # Important: Set root logger level to the lowest of its handlers
    # to allow messages to pass through to handlers.
    # Handlers then filter based on their own levels.
    lowest_level = min(numeric_file_level, numeric_console_level)
    logging.basicConfig(
        level=lowest_level,
        format=CONSOLE_FORMAT, # Default format, RichHandler will override for console
        datefmt=DATE_FORMAT,
        handlers=[]) # Start with no handlers, add them below

    # Get the root logger
    root_logger = logging.getLogger()
    # Clear any existing handlers from previous setups (e.g., if called multiple times)
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # Console Handler (Rich)
    # Use the global rich_console
    rich_handler = RichHandler(
        console=rich_console,
        show_time=False, # File logger has time
        show_level=True,
        show_path=False, # File logger has path
        markup=True,
        rich_tracebacks=True,
        tracebacks_show_locals=True,
    )
    rich_handler.setLevel(numeric_console_level)
    # Formatter for RichHandler is mostly handled by Rich itself, but can be set if needed
    # console_formatter = logging.Formatter(CONSOLE_FORMAT, datefmt=DATE_FORMAT)
    # rich_handler.setFormatter(console_formatter)
    root_logger.addHandler(rich_handler)

    # File Handler
    if not disable_file_logging:
        try:
            file_handler = logging.FileHandler(effective_log_path, encoding='utf-8')
            file_handler.setLevel(numeric_file_level)
            file_formatter = logging.Formatter(FILE_FORMAT, datefmt=DATE_FORMAT)
            file_handler.setFormatter(file_formatter)
            root_logger.addHandler(file_handler)
        except Exception as e:
            # Fallback to console if file logging fails
            root_logger.error(f"Failed to configure file logging at {effective_log_path}: {e}. Logging to console only.")
            # If rich_handler failed too, this might not appear.

    # Suppress overly verbose logs from libraries if needed
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    logger.debug(f"Logging setup complete. Console: {console_log_level}, File: {file_log_level} at {effective_log_path}")


async def view_log_file(log_file_path: Path, lines: Optional[int], follow: bool, raw: bool) -> None:
    """Displays log file content to the console."""
    if not log_file_path.exists():
        rich_console.print(f"[bold red]Error: Log file not found at {log_file_path}[/bold red]")
        return

    try:
        if lines:
            # Read last N lines
            with open(log_file_path, "r", encoding="utf-8") as f:
                log_lines = f.readlines()
            for line in log_lines[-lines:]:
                rich_console.print(Text.from_ansi(line.rstrip()) if raw else line.rstrip())
        elif follow:
            rich_console.print(f"[cyan]Following log file: {log_file_path} (Ctrl+C to stop)[/cyan]")
            with open(log_file_path, "r", encoding="utf-8") as f:
                # Go to the end of the file
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if not line:
                        await asyncio.sleep(0.1) # Wait for new lines
                        continue
                    rich_console.print(Text.from_ansi(line.rstrip()) if raw else line.rstrip())
        else:
            # Print entire file
            with open(log_file_path, "r", encoding="utf-8") as f:
                for line in f:
                    rich_console.print(Text.from_ansi(line.rstrip()) if raw else line.rstrip())
    except KeyboardInterrupt:
        rich_console.print("\n[cyan]Stopped following log file.[/cyan]")
    except Exception as e:
        rich_console.print(f"[bold red]Error reading log file: {e}[/bold red]")

