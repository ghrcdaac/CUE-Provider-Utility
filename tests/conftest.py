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
