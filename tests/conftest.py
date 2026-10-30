"""Test isolation: point the home directory at a throwaway folder.

The CLI reads and creates ``~/.cue-upload/config.toml`` at import time (help
texts show default paths), so this must run before ``cue_provider_utility`` is
imported. pytest loads ``conftest.py`` before any test module.
"""

import os
import tempfile
from pathlib import Path

_FAKE_HOME = Path(tempfile.mkdtemp(prefix="cue-upload-test-home-"))
os.environ["HOME"] = str(_FAKE_HOME)
os.environ["USERPROFILE"] = str(_FAKE_HOME)
os.environ.pop("CUE_UPLOAD_API_TOKEN", None)

FAKE_HOME = _FAKE_HOME


import keyring  # noqa: E402
import pytest  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402


class MemoryKeyring(KeyringBackend):
    """In-memory vault so tests never touch the real OS keyring."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict = {}

    def get_password(self, service: str, username: str):  # type: ignore[no-untyped-def]
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        from keyring.errors import PasswordDeleteError

        if (service, username) not in self.store:
            raise PasswordDeleteError("not found")
        del self.store[(service, username)]


keyring.set_keyring(MemoryKeyring())


@pytest.fixture(autouse=True)
def memory_keyring():  # type: ignore[no-untyped-def]
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    yield kr
