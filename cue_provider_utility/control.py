"""Control channel on stdin for ``--json-events`` mode.

The desktop app writes one JSON command per line: ``{"cmd":"start"}`` answers
``confirm_required``. End of input (the parent went away) counts as cancel.
"""

import asyncio
import json
import logging
import sys
from typing import IO, Optional

logger = logging.getLogger(__name__)


def read_command(stream: Optional[IO[str]] = None) -> Optional[str]:
    """Blocks for the next valid command line; returns ``None`` on EOF."""
    stream = stream or sys.stdin
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line).get("cmd")
        except (ValueError, AttributeError):
            logger.warning("Ignoring malformed control line on stdin.")
            continue
        if isinstance(cmd, str):
            return cmd
        logger.warning("Ignoring control line without a 'cmd'.")


def _wait_for_start_blocking(stream: Optional[IO[str]] = None) -> bool:
    while True:
        cmd = read_command(stream)
        if cmd is None or cmd == "cancel":
            return False
        if cmd == "start":
            return True
        logger.warning(f"Unknown control command '{cmd}' ignored.")


async def wait_for_start(stream: Optional[IO[str]] = None) -> bool:
    """Waits for ``start`` (True) or ``cancel``/EOF (False) without blocking the loop."""
    return await asyncio.to_thread(_wait_for_start_blocking, stream)
