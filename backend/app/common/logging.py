"""Structured (JSON) logging with request-scoped context.

Context such as request_id / user_id / company_id is stored in contextvars and attached
to every log record automatically. Callers must never pass passwords, tokens, secrets or
raw transcript text into log messages or `extra`.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
company_id_var: ContextVar[str | None] = ContextVar("company_id", default=None)

_CONTEXT_VARS = {
    "request_id": request_id_var,
    "user_id": user_id_var,
    "company_id": company_id_var,
}

# Attributes present on every LogRecord; anything else was passed via `extra`.
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}

# Defence in depth: keys that must never reach log output even if passed by mistake.
_REDACTED_KEYS = {"password", "token", "access_token", "refresh_token", "secret", "authorization"}


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in _CONTEXT_VARS.items():
            if not hasattr(record, name):
                setattr(record, name, var.get())
        return True


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    """Fields passed via `extra` (plus context vars), with sensitive keys redacted."""
    fields: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _RESERVED or key.startswith("_") or value is None:
            continue
        fields[key] = "[REDACTED]" if key.lower() in _REDACTED_KEYS else value
    return fields


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **_extra_fields(record),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable development format: `time level logger msg key=value ...`."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        fields = " ".join(f"{k}={v}" for k, v in _extra_fields(record).items())
        line = f"{ts} {record.levelname:<7} {record.name} {record.getMessage()} {fields}".rstrip()
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # uvicorn's own access log would duplicate ours and includes query strings.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
