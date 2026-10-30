"""Archives dist/cue-upload/ as cue-upload-<target>.(zip|tar.gz) and writes SHA256SUMS.

Usage: python packaging/package.py <target-triple>
"""

import hashlib
import shutil
import sys
from pathlib import Path

target = sys.argv[1]
dist = Path("dist")
fmt = "zip" if "windows" in target else "gztar"
archive = Path(
    shutil.make_archive(str(dist / f"cue-upload-{target}"), fmt, dist, "cue-upload")
)
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
(dist / "SHA256SUMS").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
print(f"{digest}  {archive.name}")
