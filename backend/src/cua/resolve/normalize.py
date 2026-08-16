"""Text normalization applied before any comparison.

The normalizer list lives in the artifact, not in this module's defaults. That is
the difference between "replay compares strings the way the recording did" and
"replay compares strings the way whatever version of the engine is deployed today
does". Same reason a migration records its schema version.

Every function here is total and idempotent: `f(f(x)) == f(x)`, and no input
raises. A normalizer that can throw turns a text comparison into a crash.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from ..schema import Normalizer


def casefold(s: str) -> str:
    raise NotImplementedError


def collapse_ws(s: str) -> str:
    """Runs of whitespace to a single space, trimmed.

    OCR line boxes routinely include column padding as spaces.
    """
    raise NotImplementedError


def strip_currency(s: str) -> str:
    """`$1,234.56` -> `1234.56`. Handles leading symbols, thousands separators and
    parenthesized negatives, which US financial UIs use for debits."""
    raise NotImplementedError


def strip_punct(s: str) -> str:
    raise NotImplementedError


def strip_ellipsis(s: str) -> str:
    """Drops a trailing `...` or `…`.

    Truncated cell text is the single most common reason a correct predicate fails
    to match. Note the asymmetry this implies: after stripping, a truncated value
    can only ever be compared by prefix, never by equality — `cell_equals` against
    a truncated cell is unanswerable and must not silently return False.
    """
    raise NotImplementedError


def digits_only(s: str) -> str:
    raise NotImplementedError


def date_iso(s: str) -> str:
    """Best-effort date normalization to `YYYY-MM-DD`.

    US back-office apps mix `MM/DD/YYYY`, `MM-DD-YY` and `Mon D, YYYY`, often on
    the same screen. Returns the input unchanged when it cannot parse — a
    normalizer must not invent data.
    """
    raise NotImplementedError


_FUNCS: dict[Normalizer, Callable[[str], str]] = {
    Normalizer.CASEFOLD: casefold,
    Normalizer.COLLAPSE_WS: collapse_ws,
    Normalizer.STRIP_CURRENCY: strip_currency,
    Normalizer.STRIP_PUNCT: strip_punct,
    Normalizer.STRIP_ELLIPSIS: strip_ellipsis,
    Normalizer.DIGITS_ONLY: digits_only,
    Normalizer.DATE_ISO: date_iso,
}


def apply(s: str, normalizers: Iterable[Normalizer]) -> str:
    """Apply in the declared order. Order matters: strip_currency before
    strip_punct, or the decimal point is gone before the number is parsed."""
    raise NotImplementedError
