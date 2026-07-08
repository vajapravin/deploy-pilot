"""Structured logging configuration and sensitive data filter."""

from __future__ import annotations

import logging
import sys

from app.services.docker_service import redact_sensitive_output


class SensitiveDataFilter(logging.Filter):
    """Log filter that automatically redacts secrets and tokens from log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_sensitive_output(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: (redact_sensitive_output(str(v)) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_sensitive_output(str(v)) if isinstance(v, str) else v
                    for v in record.args
                )
        return True


def setup_logging(log_level: str = "INFO") -> None:
    """Configure application logging with redaction filter and standard format."""
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level.upper())

    # Avoid duplicate handlers on re-initialization
    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
        handler.setFormatter(formatter)
        handler.addFilter(SensitiveDataFilter())
        root_logger.addHandler(handler)
