"""Time, in one place.

Every timestamp in an artifact, a result, an evidence record and an intervention
is produced here. One function rather than scattered `datetime.now()` calls
because two of them are auditable records of when a machine touched a member's
account: they must be UTC, they must be unambiguous, and they must all agree on
format.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone


def now_iso() -> str:
    """Current UTC time as `2026-08-16T19:41:07.123456+00:00`."""
    return datetime.now(timezone.utc).isoformat()


def monotonic_ms() -> float:
    """Elapsed-time source for durations and timeouts.

    Separate from `now_iso` on purpose: wall-clock time can step backwards (NTP),
    and a timeout that can go backwards is a timeout that can hang.
    """
    return time.monotonic() * 1000.0
