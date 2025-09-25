# In cue_provider_utility/auth_handler.py
# (This is the complete updated file content)

import os
import logging
from typing import Optional, Tuple
from urllib.parse import urlparse

from .models import AppConfig, GlobalArgs
from .netrc_handler import get_key_from_netrc

logger = logging.getLogger(__name__)
ENV_VAR_NAME = "CUE_UPLOAD_API_TOKEN"

def get_key_from_env() -> Optional[str]:
    """Retrieves API key from the environment variable."""
    return os.getenv(ENV_VAR_NAME)

def get_key_from_config(config: AppConfig) -> Optional[str]:
    """Retrieves API key from the application configuration."""
    return config.api_key

def get_api_key(
    key_from_cli: Optional[str],
    config: AppConfig,
    global_args: GlobalArgs
) -> Tuple[Optional[str], Optional[str]]:
    """
    Retrieves the authentication API key based on the defined order of precedence.
    Returns the key and its source.
    
    Priority Order:
    1. Command-line argument (--token)
    2. Environment variable (CUE_UPLOAD_API_TOKEN)
    3. .netrc file
    4. Configuration file (config.toml)
    """
    if key_from_cli:
        return key_from_cli, "command-line argument"

    key_from_environ = get_key_from_env()
    if key_from_environ:
        return key_from_environ, "environment variable"

    # Get hostname from environment URL for .netrc lookup
    env = global_args.env_cli or config.default_env
    try:
        base_url_obj = getattr(config.environments, env)
        hostname = urlparse(str(base_url_obj)).hostname
        if hostname:
            key_from_netrc_file = get_key_from_netrc(hostname)
            if key_from_netrc_file:
                return key_from_netrc_file, ".netrc file"
    except Exception as e:
        logger.warning(f"Could not look up hostname for .netrc: {e}")

    key_from_cfg = get_key_from_config(config)
    if key_from_cfg:
        return key_from_cfg, "configuration file"

    logger.info("No API key found from any source.")
    return None, None