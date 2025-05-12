"""
Manages ignored file patterns for folder uploads.
Combines system default ignores with user-defined patterns from config.
Provides CLI commands to manage user-defined ignore list.
"""
import fnmatch
from pathlib import Path
from typing import List, Optional, Tuple, Set
import logging

from .config_manager import get_user_ignore_patterns, set_user_ignore_patterns, get_config_file_path
from .exceptions import IgnoredFileError, ConfigError

logger = logging.getLogger(__name__)

# System default ignore patterns (glob-style)
# These are typically OS-specific or common development nuisance files.
# Case-insensitivity for matching should be handled by the matching function.
SYSTEM_DEFAULT_IGNORES: Set[str] = {
    ".DS_Store",
    "Thumbs.db",
    "*.tmp",
    "~$*", # Word temporary files
    "__pycache__/", # Python bytecode cache
    "*.pyc",
    "*.pyo",
    ".pytest_cache/",
    ".mypy_cache/",
    ".coverage",
    "*.swp", # Vim swap files
    "*.swo", # Vim swap files
    ".idea/", # JetBrains IDEs
    ".vscode/", # VS Code
    # Add more common patterns as needed
}

def get_active_ignore_patterns(config_path_override: Optional[Path] = None) -> Tuple[Set[str], List[str]]:
    """
    Returns a tuple of (system_default_patterns, user_defined_patterns).
    """
    user_patterns = get_user_ignore_patterns(config_path_override)
    return SYSTEM_DEFAULT_IGNORES, user_patterns

def list_ignore_patterns(config_path_override: Optional[Path] = None) -> Tuple[List[str], List[str]]:
    """
    Returns sorted lists of system and user ignore patterns for display.
    """
    system_set, user_list = get_active_ignore_patterns(config_path_override)
    return sorted(list(system_set)), sorted(user_list)


def add_user_ignore_pattern(pattern: str, config_path_override: Optional[Path] = None) -> bool:
    """Adds a pattern to the user's ignore list in config. Returns True if added, False if already exists."""
    if not pattern:
        raise IgnoredFileError("Ignore pattern cannot be empty.")
    
    user_patterns = get_user_ignore_patterns(config_path_override)
    if pattern in user_patterns:
        logger.info(f"Pattern '{pattern}' already in user ignore list.")
        return False
    
    user_patterns.append(pattern)
    try:
        set_user_ignore_patterns(user_patterns, config_path_override)
        logger.info(f"Pattern '{pattern}' added to user ignore list.")
        return True
    except ConfigError as e:
        raise IgnoredFileError(f"Failed to save updated ignore patterns: {e}", original_exception=e)

def remove_user_ignore_pattern(pattern: str, config_path_override: Optional[Path] = None) -> bool:
    """Removes a pattern from the user's ignore list. Returns True if removed, False if not found."""
    if not pattern:
        raise IgnoredFileError("Pattern to remove cannot be empty.")

    user_patterns = get_user_ignore_patterns(config_path_override)
    if pattern not in user_patterns:
        logger.info(f"Pattern '{pattern}' not found in user ignore list.")
        return False
    
    user_patterns.remove(pattern)
    try:
        set_user_ignore_patterns(user_patterns, config_path_override)
        logger.info(f"Pattern '{pattern}' removed from user ignore list.")
        return True
    except ConfigError as e:
        raise IgnoredFileError(f"Failed to save updated ignore patterns: {e}", original_exception=e)

def reset_user_ignore_patterns(config_path_override: Optional[Path] = None) -> None:
    """Clears all user-defined ignore patterns from the config."""
    try:
        set_user_ignore_patterns([], config_path_override)
        logger.info("User-defined ignore patterns have been reset.")
    except ConfigError as e:
        raise IgnoredFileError(f"Failed to reset ignore patterns: {e}", original_exception=e)


def is_path_ignored(
    path_to_check: Path,
    base_path: Path, # The root path from which the scan started, for relative matching
    config_path_override: Optional[Path] = None
) -> bool:
    """
    Checks if a given path (file or directory) matches any of the active ignore patterns.
    Patterns are matched against the path relative to the base_path, and also against the name.
    Matching is case-insensitive for basenames, but patterns with slashes are path-sensitive.
    """
    system_patterns, user_patterns = get_active_ignore_patterns(config_path_override)
    all_patterns = system_patterns.union(set(user_patterns))

    # Path relative to the original scan root (e.g., "subfolder/file.log" or "node_modules/")
    try:
        relative_path_str = str(path_to_check.relative_to(base_path))
    except ValueError: 
        # path_to_check is not under base_path, should not happen if base_path is scan root
        # Or path_to_check IS base_path, in which case relative_path is "."
        if path_to_check == base_path:
            relative_path_str = "." # Or handle as special case if needed
        else: # Should not occur in normal folder scanning context
            relative_path_str = path_to_check.name


    path_name_lower = path_to_check.name.lower()

    for pattern in all_patterns:
        pattern_lower = pattern.lower()
        # Check against the name (case-insensitive)
        if fnmatch.fnmatchcase(path_name_lower, pattern_lower):
            logger.debug(f"Path '{path_to_check.name}' matched name pattern '{pattern}'. Ignoring.")
            return True
        
        # Check against relative path (case-sensitive for paths, but pattern often lowercased)
        # For patterns with slashes, treat them as path patterns
        if '/' in pattern or '\\' in pattern:
            # Normalize slashes in pattern for matching against relative_path_str
            normalized_pattern = pattern.replace('\\', '/')
            # fnmatch is typically for basenames. For paths, pathlib's match or custom logic is better.
            # Path.match is anchored at the beginning of the relative path part.
            # If pattern is like "dir/", it matches "dir" and "dir/subdir"
            # If pattern is like "*.log", it should be matched against name.
            # If pattern is like "build/", it should match the directory "build"
            
            # Let's use Path.match for directory-like patterns or patterns with internal slashes
            # This requires careful pattern definition (e.g., "build/" vs "build")
            # For simplicity with glob, we can check if relative_path_str starts with a dir pattern
            # or if a full relative path matches.
            # Example: pattern "node_modules/" should match "node_modules/somefile.js"
            # Example: pattern ".git/"
            if normalized_pattern.endswith('/'): # Directory pattern
                # Check if relative_path_str starts with the directory name or is the directory name
                dir_pattern_base = normalized_pattern.rstrip('/')
                if relative_path_str == dir_pattern_base or \
                   relative_path_str.startswith(dir_pattern_base + '/'):
                    logger.debug(f"Path '{relative_path_str}' matched directory pattern '{pattern}'. Ignoring.")
                    return True
            elif fnmatch.fnmatchcase(relative_path_str, pattern): # Full relative path match
                    logger.debug(f"Path '{relative_path_str}' matched relative path pattern '{pattern}'. Ignoring.")
                    return True
        # else: # Pattern without slashes, already checked against name

    return False

