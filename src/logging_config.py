import os
import re
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

_LOG_DIR = Path("logs")
_LOG_FILE = _LOG_DIR / "automation.log"

# Patterns to mask in logs
SENSITIVE_PATTERNS = [
    re.compile(r'(secret_[A-Za-z0-9_\-\.]+)', re.IGNORECASE),
    re.compile(r'(Bearer\s+[A-Za-z0-9_\-\.]+)', re.IGNORECASE),
    re.compile(r'(cookie:\s*)[^\r\n;]+', re.IGNORECASE),
    re.compile(r'([a-zA-Z0-9_\-\.]*token[a-zA-Z0-9_\-\.]*\s*[:=]\s*)[^\s,;]+', re.IGNORECASE),
    re.compile(r'([a-zA-Z0-9_\-\.]*key[a-zA-Z0-9_\-\.]*\s*[:=]\s*)[^\s,;]+', re.IGNORECASE),
]


class RedactingFormatter(logging.Formatter):
    """Formatter that strips or redacts sensitive keys, secrets, and auth tokens."""

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        redacted = original
        for pattern in SENSITIVE_PATTERNS:
            redacted = pattern.sub(r'\1[REDACTED]', redacted)
        return redacted


def setup_logger(
    name: str = "youtube_automation",
    log_level: int = logging.INFO,
    log_to_file: bool = True,
    log_file_path: Optional[Path] = None,
) -> logging.Logger:
    """Configures and returns a centralized application logger with sensitive data redaction."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(log_level)
    logger.propagate = False

    formatter = RedactingFormatter(
        fmt="[%(asctime)s] [%(levelname)s] [%(name)s:%(funcName)s:%(lineno)d] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console Handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File Handler
    if log_to_file:
        target_path = log_file_path or _LOG_FILE
        target_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            target_path,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


# Default logger instance
logger = setup_logger()
