"""Structured logging.

structlog, JSON output, `run_id` and `step_id` bound into context so every line is
correlatable without being reconstructed from timestamps.

The processor chain ends with the redactor. A log line is the easiest place in the
system to leak a member id or a typed password, precisely because it is the place
nobody reviews.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog


def _redacting(redactor: Any) -> Any:
    """Last processor before rendering, so nothing added later escapes it.

    Order is the whole point: a redactor placed before the processors that add
    context would mask the event and leave the bound fields untouched.
    """

    def processor(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
        return {
            k: (redactor.redact_text(v) if isinstance(v, str) else v)
            for k, v in event.items()
        }

    return processor


def configure(redactor: Any, level: str = "INFO") -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _redacting(redactor),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(**bind: Any) -> Any:
    """A logger with `run_id` / `step_id` already bound.

    Passed down rather than fetched globally, so a line can always be traced to
    the run that produced it without reconstructing from timestamps.
    """
    return structlog.get_logger().bind(**bind)
