"""Which API URL a run may talk to, and how TLS is verified.

``--trusted-endpoints`` (or ``CUE_UPLOAD_TRUSTED_ENDPOINTS=1``), always passed
by the desktop app, ignores ``environments.*`` from config.toml, refuses
``local`` unless ``--allow-local`` is given, and requires https. Without it the
standalone CLI behaves as before, with a one-time warning when config.toml
points an official environment at a different host.
"""

import logging
import os
import ssl
from typing import Any, Set, Union
from urllib.parse import urlparse

import click

from .models import BUILTIN_ENVIRONMENTS, EnvironmentURLs, GlobalArgs

logger = logging.getLogger(__name__)

TRUSTED_ENV_VAR = "CUE_UPLOAD_TRUSTED_ENDPOINTS"
CA_BUNDLE_ENV_VAR = "CUE_UPLOAD_CA_BUNDLE"
_warned: Set[str] = set()
_tls: Union[None, bool, ssl.SSLContext] = None


def trusted_from_env() -> bool:
    return os.environ.get(TRUSTED_ENV_VAR, "").strip().lower() in ("1", "true", "yes")


def enforce(global_args: GlobalArgs) -> None:
    """Applies the endpoint policy to ``global_args.config`` for the env in use."""
    env = (global_args.env_cli or global_args.config.default_env).lower()
    if global_args.trusted_endpoints or trusted_from_env():
        global_args.trusted_endpoints = True
        if env == "local" and not global_args.allow_local:
            raise click.UsageError(
                "The local environment is not allowed with --trusted-endpoints "
                "(pass --allow-local for development)."
            )
        global_args.config = global_args.config.model_copy(
            update={"environments": EnvironmentURLs()}
        )
        url = BUILTIN_ENVIRONMENTS.get(env, "")
        if env != "local" and urlparse(url).scheme != "https":
            raise click.UsageError(f"Refusing non-https URL for '{env}'.")
        return
    _warn_if_overridden(global_args, env)


def _warn_if_overridden(global_args: GlobalArgs, env: str) -> None:
    if env == "local" or env in _warned or env not in BUILTIN_ENVIRONMENTS:
        return
    official = urlparse(BUILTIN_ENVIRONMENTS[env]).hostname
    try:
        configured = urlparse(str(getattr(global_args.config.environments, env)))
    except Exception:
        return
    if configured.hostname and configured.hostname != official:
        _warned.add(env)
        from .logger_setup import rich_console

        text = (
            f"config.toml overrides the {env} URL to {configured.hostname}; API keys "
            "stored in the OS keyring are bound to the official host and won't be "
            "sent there."
        )
        logger.warning(text)
        rich_console.print(f"[yellow]{text}[/yellow]")


def tls_verify() -> Union[str, bool, ssl.SSLContext]:
    """TLS verification for every HTTPS client.

    ``CUE_UPLOAD_CA_BUNDLE`` (a PEM file) wins; otherwise the OS trust store via
    ``truststore`` (works behind corporate proxies), else certifi's defaults.
    """
    global _tls
    bundle = os.environ.get(CA_BUNDLE_ENV_VAR)
    if bundle:
        return bundle
    if _tls is None:
        try:
            import truststore

            _tls = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:  # not installed / unsupported platform
            _tls = True
    return _tls


def trusted_options(f: Any) -> Any:
    """Hidden ``--trusted-endpoints`` / ``--allow-local`` flags."""
    f = click.option(
        "--allow-local",
        "allow_local",
        is_flag=True,
        hidden=True,
        help="Allow the local environment with --trusted-endpoints.",
    )(f)
    f = click.option(
        "--trusted-endpoints",
        "trusted_endpoints",
        is_flag=True,
        hidden=True,
        help="Use only the built-in API URLs (desktop app).",
    )(f)
    return f
