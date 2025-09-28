# cue_provider_utility/config_manager.py

"""
Manages the application's configuration file (config.toml).
Handles loading, saving, validation, and providing access to configuration settings.
"""
import toml
from pathlib import Path
from typing import Any, Optional, List, cast
import logging

from .models import AppConfig 
from .exceptions import ConfigError

logger = logging.getLogger(__name__)

APP_NAME = "cue-upload"
DEFAULT_CONFIG_DIR = Path.home() / f".{APP_NAME}"
DEFAULT_CONFIG_FILENAME = "config.toml"

def get_config_file_path(config_path_override: Optional[Path] = None) -> Path:
    """Determines the active configuration file path."""
    if config_path_override:
        return config_path_override.resolve()
    return (DEFAULT_CONFIG_DIR / DEFAULT_CONFIG_FILENAME).resolve()

def ensure_config_exists(config_path_override: Optional[Path] = None) -> None:
    """Ensures the configuration directory and a default config file exist."""
    config_file_path = get_config_file_path(config_path_override)
    config_dir = config_file_path.parent
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        if not config_file_path.exists():
            logger.info(f"Configuration file not found at {config_file_path}. Creating with defaults.")
            default_config = AppConfig() 
            save_config(default_config, config_file_path)
    except OSError as e:
        raise ConfigError(f"Could not create config directory or file at {config_file_path}: {e}", original_exception=e)
    except Exception as e:
        raise ConfigError(f"An unexpected error occurred ensuring config exists: {e}", original_exception=e)


def load_config(config_file_path: Path) -> AppConfig:
    """Loads configuration from the TOML file and validates it."""
    if not config_file_path.exists():
        logger.warning(f"Config file {config_file_path} not found during load. Creating with defaults.")
        ensure_config_exists(config_file_path)

    try:
        with open(config_file_path, "r", encoding="utf-8") as f:
            data = toml.load(f)
        
        return AppConfig(**data)
    except toml.TomlDecodeError as e:
        logger.error(f"Error decoding TOML from {config_file_path}: {e}")
        raise ConfigError(f"Invalid TOML format in {config_file_path}.", original_exception=e)
    except Exception as e:
        logger.error(f"Error loading or validating configuration from {config_file_path}: {e}")
        raise ConfigError(f"Could not load or validate configuration: {e}", original_exception=e)

def save_config(config_data: AppConfig, config_file_path: Path) -> None:
    """Saves the configuration data to the TOML file."""
    try:
        config_dir = config_file_path.parent
        config_dir.mkdir(parents=True, exist_ok=True)
        
        config_dict = config_data.model_dump(mode='json', exclude_defaults=False, exclude_none=False)

        with open(config_file_path, "w", encoding="utf-8") as f:
            toml.dump(config_dict, f)
        logger.info(f"Configuration saved to {config_file_path}")
    except Exception as e:
        logger.error(f"Failed to save configuration to {config_file_path}: {e}")
        raise ConfigError(f"Could not save configuration: {e}", original_exception=e)

_cached_config: Optional[AppConfig] = None
_cached_config_path: Optional[Path] = None

def get_config(config_path_override: Optional[Path] = None) -> AppConfig:
    """
    Loads and returns the application configuration, using a cached version if available
    and the path hasn't changed. Ensures config file exists.
    """
    global _cached_config, _cached_config_path
    target_config_path = get_config_file_path(config_path_override)

    ensure_config_exists(target_config_path)

    if _cached_config is not None and _cached_config_path == target_config_path:
        return _cached_config
    
    config = load_config(target_config_path)
    
    _cached_config = config
    _cached_config_path = target_config_path
    return config

def save_config_value(key: str, value: Any, config_path_override: Optional[Path] = None) -> None:
    """Saves a specific key-value pair to the configuration."""
    config_file_to_use = get_config_file_path(config_path_override)
    current_config = get_config(config_file_to_use)
    
    if '.' in key: 
        parent_key, child_key = key.split('.', 1)
        parent_obj = getattr(current_config, parent_key, None)
        if parent_obj is not None and isinstance(parent_obj, object):
            if hasattr(parent_obj, child_key):
                setattr(parent_obj, child_key, value)
            elif isinstance(parent_obj, dict) and child_key in parent_obj:
                parent_obj[child_key] = value
            else:
                raise ConfigError(f"Invalid configuration key: {key}. Child key '{child_key}' not found in '{parent_key}'.")
        else:
            raise ConfigError(f"Invalid configuration key: {key}. Parent key '{parent_key}' not found or not an object.")
    elif hasattr(current_config, key):
        setattr(current_config, key, value)
    else:
        raise ConfigError(f"Invalid configuration key: {key}")

    save_config(current_config, config_file_to_use)
    
    global _cached_config, _cached_config_path
    _cached_config = None
    _cached_config_path = None


def save_api_key_to_config(key: Optional[str], config_path_override: Optional[Path] = None) -> None:
    """Specifically saves or removes the API key in the configuration."""
    # This now saves the 'api_key' field, which matches the updated AppConfig model.
    save_config_value("api_key", key, config_path_override)

def display_config_value_for_edit(prompt_text: str, current_value: Any) -> str:
    """Formats the prompt text for editing a config value, showing current value."""
    if current_value is None or (isinstance(current_value, str) and current_value == ""):
        return f"{prompt_text} (leave blank for no change)"
    return f"{prompt_text} (current: {current_value}, leave blank to keep)"

# --- User Ignore Patterns Management ---
def get_user_ignore_patterns(config_path_override: Optional[Path] = None) -> List[str]:
    """Retrieves the list of user-defined ignore patterns from config."""
    config = get_config(config_path_override)
    return cast(List[str], config.user_ignored_patterns)

def set_user_ignore_patterns(patterns: List[str], config_path_override: Optional[Path] = None) -> None:
    """Sets the list of user-defined ignore patterns in config."""
    save_config_value("user_ignored_patterns", patterns, config_path_override)