"""API keys in the OS credential vault, one entry per environment host.

Service ``cue-upload``, account = the API hostname of the environment
(``upload.earthdata.nasa.gov``, ``upload.uat.earthdata.nasa.gov``, …), the same
key the desktop app uses. Non-secret metadata (name, last 4 chars, expiry,
source) lives in ``~/.cue-upload/credentials.json``; secrets never go there.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .models import AppConfig

logger = logging.getLogger(__name__)

SERVICE = "cue-upload"
KEY_PATTERN = r"^cue_sk_[A-Za-z0-9_-]{20,}$"
METADATA_FIELDS = (
    "key_id",
    "name",
    "suffix",
    "created_at",
    "expires_at",
    "source",
    "owner_name",
)


def host_for_env(config: AppConfig, env: str) -> Optional[str]:
    """Hostname of the environment's API URL (the keyring account / .netrc machine)."""
    try:
        return urlparse(str(getattr(config.environments, env))).hostname
    except Exception:
        return None


def _keyring() -> Any:
    import keyring

    return keyring


def backend_name() -> str:
    try:
        kr = _keyring().get_keyring()
        return f"{type(kr).__module__}.{type(kr).__name__}"
    except Exception as e:  # pragma: no cover - defensive
        return f"unavailable ({e.__class__.__name__})"


def available() -> bool:
    """False when no usable OS vault exists (e.g. headless Linux)."""
    try:
        kr = _keyring().get_keyring()
    except Exception:
        return False
    mod = type(kr).__module__
    if mod.startswith("keyring.backends.fail") or mod.startswith(
        "keyring.backends.null"
    ):
        return False
    chainer = getattr(kr, "backends", None)
    if mod.startswith("keyring.backends.chainer") and not chainer:
        return False
    return True


def get_key(host: str) -> Optional[str]:
    if not host or not available():
        return None
    try:
        value = _keyring().get_password(SERVICE, host)
        return str(value) if value else None
    except Exception as e:
        logger.warning(f"Could not read the OS keyring: {e.__class__.__name__}")
        return None


def set_key(host: str, key: str) -> bool:
    """Stores the key; returns False (with a warning) if the vault can't be used."""
    if not available():
        return False
    try:
        _keyring().set_password(SERVICE, host, key)
        return True
    except Exception as e:
        logger.warning(f"Could not write to the OS keyring: {e.__class__.__name__}")
        return False


def delete_key(host: str) -> bool:
    if not available():
        return False
    try:
        _keyring().delete_password(SERVICE, host)
        return True
    except Exception:
        return False


# --- credentials.json (non-secret metadata) ---------------------------------------


def metadata_path() -> Path:
    from .config_manager import DEFAULT_CONFIG_DIR

    return DEFAULT_CONFIG_DIR / "credentials.json"


def load_metadata() -> Dict[str, Dict[str, Any]]:
    path = metadata_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_metadata(data: Dict[str, Dict[str, Any]]) -> None:
    path = metadata_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".credentials-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_metadata(host: str, replace: bool = False, **fields: Any) -> None:
    """Merges non-secret fields for ``host`` (``replace`` starts a fresh record)."""
    data = load_metadata()
    record = {} if replace else dict(data.get(host, {}))
    for k, v in fields.items():
        if k in METADATA_FIELDS and v is not None:
            record[k] = v
    data[host] = record
    _save_metadata(data)


def remove_metadata(host: str) -> None:
    data = load_metadata()
    if data.pop(host, None) is not None:
        _save_metadata(data)
