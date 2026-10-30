"""Removes secrets from text before it reaches logs or events."""

import logging
import re

_PATTERNS = [
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"cue_sk_[A-Za-z0-9_-]+"), "cue_sk_[REDACTED]"),
    (
        re.compile(r"(?i)(X-Amz-(?:Signature|Credential|Security-Token)=)[^&\s\"'<>]+"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)([\"']?(?:refresh_token|access_token|id_token)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,&}]+"
        ),
        r"\1[REDACTED]",
    ),
]


def redact(text: str) -> str:
    """Returns ``text`` with API keys, bearer tokens and presigned URL secrets removed."""
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Logging filter that redacts the fully formatted message of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = redact(message)
        if cleaned != message or record.args:
            record.msg = cleaned
            record.args = None
        return True


class RedactingFormatter(logging.Formatter):
    """Formatter that also redacts tracebacks and anything added while formatting."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
