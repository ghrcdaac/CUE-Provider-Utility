"""One catalog of user-visible outcomes, shared by the terminal and the GUI.

``report(code, **details)`` always writes the text to the log and, in
``--json-events`` mode, emits a ``notice`` event. Terminal wording printed by
the existing commands is kept as it is (no change for terminal users).
Exceptions and HTTP errors are mapped to codes only in ``map_exception``.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from . import events
from .exceptions import (
    APIRequestError,
    AuthError,
    ConfigError,
    FileProcessingError,
    IgnoredFileError,
    UploadCancelledError,
)
from .redact import redact

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Msg:
    code: str
    severity: str  # "success" | "info" | "warning" | "error"
    title: str
    text: str
    notify: bool
    retryable: bool = False
    hint: Optional[str] = None


def _m(*args: Any, **kwargs: Any) -> Msg:
    return Msg(*args, **kwargs)


CATALOG: Dict[str, Msg] = {
    m.code: m
    for m in [
        # Success
        _m(
            "UPLOAD_OK",
            "success",
            "Uploaded",
            "{name} was uploaded to {collection}.",
            False,
        ),
        _m(
            "BATCH_OK",
            "success",
            "Upload complete",
            "{succeeded} file(s) uploaded to {collection}.",
            True,
        ),
        _m(
            "RETRY_OK",
            "success",
            "Retry complete",
            "{name} was uploaded on retry.",
            False,
        ),
        # Info
        _m(
            "SCAN_DONE",
            "info",
            "Scan complete",
            "Found {files} file(s) to upload ({size}).",
            False,
        ),
        _m(
            "SKIPPED_IGNORED",
            "info",
            "Skipped",
            "{name} matches an ignore pattern and was skipped.",
            False,
        ),
        _m(
            "SKIPPED_EMPTY",
            "info",
            "Skipped",
            "{name} is empty (0 B) and was skipped.",
            False,
        ),
        _m("CANCELLED", "info", "Upload cancelled", "The upload was cancelled.", True),
        # Warning
        _m(
            "BATCH_PARTIAL",
            "warning",
            "Upload finished with errors",
            "{succeeded} uploaded, {failed} failed.",
            True,
            hint="Retry the failed files from Needs attention, or run 'cue-upload retry'.",
        ),
        _m(
            "KEY_EXPIRING",
            "warning",
            "Upload key expires soon",
            "Your upload key for {env} expires in {days} day(s).",
            True,
            hint="Renew your key in Settings.",
        ),
        _m(
            "CONFIG_KEY_SHARED",
            "warning",
            "Shared key in use",
            "Using the shared config.toml key for {env}; store a per-environment key with "
            "'cue-upload auth set-key --env {env}'.",
            False,
        ),
        # Errors
        _m(
            "AUTH_MISSING",
            "error",
            "No upload key",
            "No API key was found for {env}.",
            True,
            hint="Add a key with 'cue-upload auth set-key' or 'cue-upload configure'.",
        ),
        _m(
            "AUTH_INVALID",
            "error",
            "Upload key rejected",
            "The server rejected the API key: {detail}",
            True,
            hint="Replace or renew your key.",
        ),
        _m(
            "FORBIDDEN",
            "error",
            "Not allowed",
            "You are not allowed to do this: {detail}",
            True,
        ),
        _m(
            "COLLECTION_NOT_FOUND",
            "error",
            "Collection not found",
            "{detail}",
            True,
            hint="Check the collection name.",
        ),
        _m("COLLECTION_INACTIVE", "error", "Collection inactive", "{detail}", True),
        _m("PROVIDER_DISABLED", "error", "Provider disabled", "{detail}", True),
        _m(
            "NOT_ASSIGNED",
            "error",
            "Not assigned",
            "{detail}",
            True,
            hint="Ask your DAAC manager to assign you to this collection's provider.",
        ),
        _m(
            "NETWORK",
            "error",
            "Network problem",
            "{detail}",
            False,
            retryable=True,
            hint="Check your connection and retry.",
        ),
        _m("TIMEOUT", "error", "Request timed out", "{detail}", False, retryable=True),
        _m(
            "S3_PUT_FAILED",
            "error",
            "Transfer failed",
            "{detail}",
            False,
            retryable=True,
        ),
        _m(
            "CHECKSUM_FAILED",
            "error",
            "Checksum failed",
            "{detail}",
            False,
            retryable=True,
        ),
        _m("FILE_READ", "error", "Can't read file", "{detail}", False),
        _m("FILE_TYPE_DENIED", "error", "File type not allowed", "{detail}", False),
        _m("CONFIG", "error", "Configuration problem", "{detail}", True),
        _m(
            "SERVER_TOO_OLD",
            "error",
            "Server too old",
            "The server doesn't support this feature yet.",
            True,
        ),
        _m(
            "UPLOAD_FAILED",
            "error",
            "Upload failed",
            "{name}: {detail}",
            False,
            retryable=True,
        ),
        _m("INTERNAL", "error", "Unexpected error", "{detail}", True),
    ]
}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(code: str, **details: Any) -> str:
    msg = CATALOG.get(code) or CATALOG["INTERNAL"]
    return redact(msg.text.format_map(_SafeDict(**details)))


def report(
    code: str,
    file_id: Optional[str] = None,
    failed_entry_id: Optional[str] = None,
    **details: Any,
) -> Msg:
    """Logs the outcome and, in events mode, emits a ``notice`` event."""
    msg = CATALOG.get(code) or CATALOG["INTERNAL"]
    text = render(msg.code, **details)
    level = {
        "error": logging.ERROR,
        "warning": logging.WARNING,
    }.get(msg.severity, logging.INFO)
    if not events.enabled():
        # Terminal mode keeps its existing wording on screen; the catalog text
        # only goes to the debug log so the console output stays unchanged.
        level = logging.DEBUG
    logger.log(level, "[%s] %s", msg.code, text)
    events.notice(
        code=msg.code,
        severity=msg.severity,
        title=msg.title,
        text=text,
        notify=msg.notify,
        retryable=msg.retryable,
        hint=msg.hint,
        file_id=file_id,
        failed_entry_id=failed_entry_id,
    )
    return msg


def _root_cause(exc: BaseException) -> BaseException:
    seen = set()
    while id(exc) not in seen:
        seen.add(id(exc))
        nxt = getattr(exc, "original_exception", None) or exc.__cause__
        if nxt is None or isinstance(exc, APIRequestError) and exc.status_code:
            break
        exc = nxt
    return exc


def map_exception(exc: BaseException) -> str:
    """Maps an exception (incl. HTTP status and backend ``detail``) to a catalog code."""
    root = _root_cause(exc)
    text = str(root).lower()
    if isinstance(root, UploadCancelledError):
        return "CANCELLED"
    if isinstance(root, AuthError):
        return "AUTH_INVALID"
    if isinstance(root, APIRequestError) and root.status_code:
        status = root.status_code
        if status == 401:
            return "AUTH_INVALID"
        if status in (403, 404, 409, 422, 400):
            if "not assigned" in text or "not mapped" in text:
                return "NOT_ASSIGNED"
            if "inactive" in text:
                return "COLLECTION_INACTIVE"
            if "provider" in text and "disabled" in text:
                return "PROVIDER_DISABLED"
            if "collection" in text and "not found" in text:
                return "COLLECTION_NOT_FOUND"
            if status == 403:
                return "FORBIDDEN"
            if status == 404:
                return "COLLECTION_NOT_FOUND"
        if "checksum" in text:
            return "CHECKSUM_FAILED"
        return "UPLOAD_FAILED"
    if isinstance(root, httpx.TimeoutException) or "timed out" in text:
        return "TIMEOUT"
    if isinstance(root, httpx.HTTPStatusError):
        return "S3_PUT_FAILED"
    if isinstance(root, (httpx.RequestError, APIRequestError)):
        return (
            "NETWORK"
            if "network" in text or isinstance(root, httpx.RequestError)
            else "UPLOAD_FAILED"
        )
    if isinstance(root, ConfigError):
        return "CONFIG"
    if isinstance(root, FileProcessingError):
        if "type" in text or "denied" in text or "not allowed" in text:
            return "FILE_TYPE_DENIED"
        if "checksum" in text:
            return "CHECKSUM_FAILED"
        return "FILE_READ"
    if isinstance(root, IgnoredFileError):
        return "SKIPPED_IGNORED"
    if isinstance(root, OSError):
        return "FILE_READ"
    if "checksum" in text:
        return "CHECKSUM_FAILED"
    return "UPLOAD_FAILED"


def report_exception(
    exc: BaseException,
    file_id: Optional[str] = None,
    failed_entry_id: Optional[str] = None,
    **details: Any,
) -> Msg:
    """Reports an exception through the catalog (code chosen by ``map_exception``)."""
    detail = str(_root_cause(exc)) or exc.__class__.__name__
    details.setdefault("detail", detail)
    details.setdefault("name", "")
    return report(
        map_exception(exc), file_id=file_id, failed_entry_id=failed_entry_id, **details
    )
