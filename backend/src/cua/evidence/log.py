"""Structured logging.

structlog, JSON output, `run_id` and `step_id` bound into context so every line is
correlatable without being reconstructed from timestamps.

The processor chain ends with the redactor. A log line is the easiest place in the
system to leak a member id or a typed password, precisely because it is the place
nobody reviews.
"""

from __future__ import annotations

from typing import Any


def configure(redactor: Any, level: str = "INFO") -> None:
    raise NotImplementedError


def get_logger(**bind: Any) -> Any:
    raise NotImplementedError
