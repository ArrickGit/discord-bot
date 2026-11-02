"""Logging with rotating files and secret redaction."""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

# Discord bot tokens have a recognisable three-part shape; redact them even if
# they are not the configured token (e.g. pasted into an error message).
_TOKEN_PATTERN = re.compile(r"[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,40}")


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        # Longest first so a secret containing another is fully replaced.
        self._secrets = sorted({s for s in secrets if len(s) >= 6}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return _TOKEN_PATTERN.sub("[REDACTED]", text)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self.redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        return True


def setup_logging(log_dir: Path, level: str, secrets: list[str]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    redactor = RedactingFilter(secrets)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    for handler in (console, file_handler):
        handler.addFilter(redactor)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(file_handler)

    # discord.py is chatty at DEBUG; keep its gateway noise down unless asked.
    if level != "DEBUG":
        logging.getLogger("discord").setLevel(logging.WARNING)
        logging.getLogger("discord.voice_state").setLevel(logging.WARNING)
