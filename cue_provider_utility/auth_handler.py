"""
Handles authentication token retrieval from various sources.
Sources: CLI argument, config file, environment variable, .netrc file.
"""
import os
import netrc
from pathlib import Path
from typing import Optional
import logging

from .models import AppConfig
from .exceptions import AuthError

logger = logging.getLogger(__name__)

# Order of precedence for token retrieval:
# 1. CLI argument (handled directly in main.py by passing token_cli)
# 2. Config file (api_token field)
# 3. Environment variable (CUE_UPLOAD_API_TOKEN)
# 4. .netrc file

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
    The backend API hostname is needed to look up the entry.
    """
    if not hostname:
        logger.debug("Hostname not provided for .netrc lookup, skipping.")
        return None
    try:
        netrc_path = Path.home() / ".netrc"
        if not netrc_path.exists():
            logger.debug(".netrc file not found.")
            return None
        
        # Ensure .netrc has restricted permissions (optional check, platform-dependent)
        # stat_info = netrc_path.stat()
        # if sys.platform != "win32" and (stat_info.st_mode & 0o077):
        #     logger.warning(f"Permissions for .netrc file ({netrc_path}) are too open. Consider restricting to user-only.")

        auth_info = netrc.netrc(str(netrc_path)).authenticators(hostname)
        if auth_info:
            # .netrc stores: login, account, password
            # We'll assume the token is stored in the 'password' field.
            # The 'login' field could be a username or an API key ID.
            token = auth_info[2] 
            if token:
                logger.debug(f"Auth token found in .netrc file for host {hostname}.")
                return token
            else:
                logger.debug(f"Password (token) field empty in .netrc for host {hostname}.")
        else:
            logger.debug(f"No .netrc entry found for host {hostname}.")
    except netrc.NetrcParseError as e:
        logger.warning(f"Could not parse .netrc file: {e}. Skipping .netrc lookup.")
    except FileNotFoundError: # Should be caught by .exists() but as a safeguard
        logger.debug(".netrc file not found (redundant check).")
    except Exception as e:
        logger.error(f"An unexpected error occurred reading .netrc: {e}")
    return None

def get_auth_token(
    token_from_cli: Optional[str],
    config: AppConfig,
    # api_client needed to get hostname for .netrc, or pass hostname directly
    # For now, let's assume we might need to get hostname from config.environments.selected_env_url
    selected_env_url: Optional[str] = None 
) -> Optional[str]:
    """
    Retrieves the authentication token based on the defined order of precedence.
    """
    # 1. CLI argument
    if token_from_cli:
        logger.debug("Using auth token from CLI argument.")
        return token_from_cli

    # 2. Config file
    token_from_cfg = get_token_from_config(config)
    if token_from_cfg:
        return token_from_cfg

    # 3. Environment variable
    token_from_environ = get_token_from_env()
    if token_from_environ:
        return token_from_environ

    # 4. .netrc file
    # To use .netrc, we need the hostname of the API server for the selected environment.
    # This requires knowing the selected environment.
    # This logic might be better placed where the ApiClient is instantiated,
    # or the selected_env_url should be reliably passed here.
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
        logger.debug("Skipping .netrc lookup as target API hostname is not determined.")

    logger.debug("No auth token found from any source.")
    return None

