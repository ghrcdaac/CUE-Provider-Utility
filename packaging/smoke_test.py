"""Smoke test for a built ``cue-upload`` binary (no network upload happens).

Usage: python packaging/smoke_test.py dist/cue-upload/cue-upload[.exe]

1. ``_gui capabilities --json`` returns the protocol handshake.
2. ``upload -P tests/fixtures --json-events`` scans, asks for confirmation,
   gets ``{"cmd":"cancel"}`` on stdin, and exits 130 with ``batch_done``.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DUMMY_KEY = "cue_sk_smoketestsmoketestsmoke0000"


def main() -> int:
    exe = Path(sys.argv[1]).resolve()
    repo = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as home:
        env = dict(os.environ, HOME=home, USERPROFILE=home)
        env.pop("CUE_UPLOAD_API_TOKEN", None)
        cfg = Path(home) / "config.toml"

        out = subprocess.run(
            [str(exe), "--config", str(cfg), "_gui", "capabilities", "--json"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        caps = json.loads(out.stdout)
        assert caps["protocol"] == 1 and "json-events" in caps["features"], caps
        print(f"capabilities OK: {caps['cli_version']} ({caps['keyring_backend']})")

        proc = subprocess.Popen(
            [
                str(exe),
                "--config",
                str(cfg),
                "upload",
                "-P",
                str(repo / "tests" / "fixtures"),
                "-c",
                "test",
                "-t",
                DUMMY_KEY,
                "--json-events",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert proc.stdin and proc.stdout
        seen = []
        for line in proc.stdout:
            event = json.loads(line)
            seen.append(event["type"])
            if event["type"] == "confirm_required":
                proc.stdin.write('{"cmd":"cancel"}\n')
                proc.stdin.flush()
        code = proc.wait(timeout=60)
        assert seen[0] == "hello" and "scan_summary" in seen, seen
        assert "confirm_required" in seen and seen[-1] == "batch_done", seen
        assert code == 130, (code, proc.stderr.read() if proc.stderr else "")
        print(f"events OK: {' > '.join(seen)} (exit {code})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
