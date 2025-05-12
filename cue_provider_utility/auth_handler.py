"""
Handles authentication token retrieval from various sources.
Sources: CLI argument, config file, environment variable, .netrc file.
"""
import os
import netrc
from pathlib import Path
from typing import Optional 
import logging
import sys 

from .models import AppConfig
from .exceptions import AuthError

logger = logging.getLogger(__name__)

ENV_VAR_NAME = "CUE_UPLOAD_API_TOKEN"

def get_token_from_env() -> Optional[str]:
    """Retrieves token from the environment variable."""
    token = os.getenv(ENV_VAR_NAME)
    if token:
        logger.debug(f"Auth token found in environment variable {ENV_VAR_NAME}.")
    return token

def get_token_from_config(config: AppConfig) -> Optional[str]:
    """Retrieves token from the application configuration."""
    if config.api_token:
        logger.debug("Auth token found in configuration file.")
    return config.api_token

def get_token_from_netrc(hostname: Optional[str] = None) -> Optional[str]:
    """
    Retrieves token (password field) from .netrc file for a given hostname.
    """
    if not hostname:
        logger.debug("Hostname not provided for .netrc lookup, skipping.")
        return None
    try:
        # Construct path to .netrc. For Windows, _netrc is also common.
        # Python's netrc module handles finding the correct file.
        # Forcing a specific path might be needed if default resolution fails.
        # netrc_path = Path.home() / ".netrc" # or "_netrc" on Windows
        # if not netrc_path.exists():
        #     logger.debug(f".netrc (or _netrc) file not found in home directory: {Path.home()}")
        #     return None
        
        # netrc.netrc() will try to find the file itself.
        auth_info = netrc.netrc().authenticators(hostname) # May raise FileNotFoundError if no .netrc
        if auth_info:
            token = auth_info[2] 
            if token:
                logger.debug(f"Auth token found in .netrc file for host {hostname}.")
                return token
            else:
                logger.debug(f"Password (token) field empty in .netrc for host {hostname}.")
        else:
            logger.debug(f"No .netrc entry found for host {hostname}.")
    except FileNotFoundError:
        logger.debug(".netrc file not found by netrc module.")
    except netrc.NetrcParseError as e:
        logger.warning(f"Could not parse .netrc file: {e}. Skipping .netrc lookup.")
    except Exception as e: # Catch any other errors during .netrc processing
        logger.error(f"An unexpected error occurred reading .netrc: {e}")
    return None

def get_auth_token(
    token_from_cli: Optional[str],
    config: AppConfig,
    selected_env_url: Optional[str] = None 
) -> Optional[str]:
    """
    Retrieves the authentication token based on the defined order of precedence.
    """
    if token_from_cli:
        logger.debug("Using auth token from CLI argument.")
        return token_from_cli

    token_from_cfg = get_token_from_config(config)
    if token_from_cfg:
        return token_from_cfg

    token_from_environ = get_token_from_env()
    if token_from_environ:
        return token_from_environ

    hostname_for_netrc = None
    if selected_env_url:
        from urllib.parse import urlparse
        try:
            parsed_url = urlparse(selected_env_url)
            hostname_for_netrc = parsed_url.hostname
        except Exception as e:
            logger.warning(f"Could not parse selected_env_url '{selected_env_url}' for .netrc hostname: {e}")
    
    if hostname_for_netrc:
        token_from_nrc = get_token_from_netrc(hostname_for_netrc)
        if token_from_nrc:
            return token_from_nrc
    else:
        logger.debug("Skipping .netrc lookup as target API hostname is not determined or URL was not provided.")

    logger.info("No auth token found from any source.") # Changed to info if it's a common case
    return None
