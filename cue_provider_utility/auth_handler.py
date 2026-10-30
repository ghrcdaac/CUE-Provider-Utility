# cue_provider_utility/auth_handler.py

import os
import logging
from typing import Dict, List, Optional, Tuple

from .models import AppConfig, GlobalArgs
from .netrc_handler import get_key_from_netrc
from . import credential_store

logger = logging.getLogger(__name__)
ENV_VAR_NAME = "CUE_UPLOAD_API_TOKEN"

SOURCE_CLI = "command-line argument"
SOURCE_ENV = "environment variable"
SOURCE_KEYRING = "OS keyring"
SOURCE_NETRC = ".netrc file"
SOURCE_CONFIG = "configuration file"


def get_key_from_env() -> Optional[str]:
    """Retrieves API key from the environment variable."""
    return os.getenv(ENV_VAR_NAME)


def get_key_from_config(config: AppConfig) -> Optional[str]:
    """Retrieves API key from the application configuration."""
    return config.api_key


_warned_shared: set = set()


def _warn_shared_config_key(env: str) -> None:
    from . import events, messages

    if env in _warned_shared:
        return
    _warned_shared.add(env)
    from .logger_setup import rich_console

    if events.enabled():
        messages.report("CONFIG_KEY_SHARED", env=env)
    else:
        rich_console.print(
            f"[yellow]Using the shared config.toml key for {env}; store a "
            f"per-environment key with `cue-upload auth set-key --env {env}`.[/yellow]"
        )


def keys_by_source(config: AppConfig, env: str) -> Dict[str, bool]:
    """Which stored sources hold a key for ``env`` (keyring, .netrc, config.toml)."""
    host = credential_store.host_for_env(config, env)
    return {
        SOURCE_KEYRING: bool(host and credential_store.get_key(host)),
        SOURCE_NETRC: bool(host and get_key_from_netrc(host)),
        SOURCE_CONFIG: bool(get_key_from_config(config)),
    }


def get_api_key(
    key_from_cli: Optional[str],
    config: AppConfig,
    global_args: GlobalArgs,
    warn_shared: bool = True,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Retrieves the authentication API key based on the defined order of precedence.
    Returns the key and its source.

    Priority Order:
    1. Command-line argument (--token)
    2. Environment variable (CUE_UPLOAD_API_TOKEN)
    3. OS keyring (service "cue-upload", account = API hostname of the environment)
    4. .netrc file (machine = API hostname of the environment)
    5. Configuration file (config.toml, one shared key)
    """
    if key_from_cli:
        return key_from_cli, SOURCE_CLI

    key_from_environ = get_key_from_env()
    if key_from_environ:
        return key_from_environ, SOURCE_ENV

    # Keyring and .netrc are looked up by the hostname of the environment in use,
    # so a key for one environment is never sent to another.
    env = global_args.env_cli or config.default_env
    hostname = credential_store.host_for_env(config, env)
    if hostname:
        key_from_vault = credential_store.get_key(hostname)
        if key_from_vault:
            return key_from_vault, SOURCE_KEYRING
        try:
            key_from_netrc_file = get_key_from_netrc(hostname)
            if key_from_netrc_file:
                return key_from_netrc_file, SOURCE_NETRC
        except Exception as e:
            logger.warning(f"Could not look up hostname for .netrc: {e}")
    else:
        logger.warning(f"Could not determine the API host for environment '{env}'.")

    key_from_cfg = get_key_from_config(config)
    if key_from_cfg:
        if warn_shared and env != config.default_env:
            _warn_shared_config_key(env)
        return key_from_cfg, SOURCE_CONFIG

    logger.info("No API key found from any source.")
    return None, None


__all__: List[str] = ["get_api_key", "keys_by_source", "ENV_VAR_NAME"]
