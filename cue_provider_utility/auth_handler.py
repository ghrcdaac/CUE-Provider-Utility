# In cue_provider_utility/auth_handler.py
# (This is the complete updated file content)

"""
Handles authentication token retrieval from various sources.
Sources: CLI argument, config file, environment variable.
"""
import os
import logging
from typing import Optional

from .models import AppConfig
from .exceptions import AuthError

logger = logging.getLogger(__name__)

# The environment variable name remains the same for backward compatibility
ENV_VAR_NAME = "CUE_UPLOAD_API_TOKEN"

def get_key_from_env() -> Optional[str]:
    """Retrieves API key from the environment variable."""
    key = os.getenv(ENV_VAR_NAME)
    if key:
        logger.debug(f"API key found in environment variable {ENV_VAR_NAME}.")
    return key

def get_key_from_config(config: AppConfig) -> Optional[str]:
    """Retrieves API key from the application configuration."""
    # The config field was updated from 'api_token' to 'api_key'
    if config.api_key:
        logger.debug("API key found in configuration file.")
    return config.api_key

def get_api_key(
    key_from_cli: Optional[str],
    config: AppConfig
) -> Optional[str]:
    """
    Retrieves the authentication API key based on the defined order of precedence.
    
    Priority Order:
    1. Command-line argument
    2. Configuration file
    3. Environment variable
    """
    if key_from_cli:
        logger.debug("Using API key from CLI argument.")
        return key_from_cli

    key_from_cfg = get_key_from_config(config)
    if key_from_cfg:
        return key_from_cfg

    key_from_environ = get_key_from_env()
    if key_from_environ:
        return key_from_environ

    logger.info("No API key found from any source.")
    return None