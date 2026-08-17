"""Text normalization applied before any comparison.

The normalizer list lives in the artifact, not in this module's defaults. That is
the difference between "replay compares strings the way the recording did" and
"replay compares strings the way whatever version of the engine is deployed today
does". Same reason a migration records its schema version.

Every function here is total and idempotent: `f(f(x)) == f(x)`, and no input
raises. A normalizer that can throw turns a text comparison into a crash.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from datetime import datetime

from ..schema import Normalizer

_WS = re.compile(r"\s+")
_CURRENCY_SYMBOL = re.compile(r"[$€£¥]")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(\D|$))")
_PARENTHESIZED = re.compile(r"^\((.*)\)$", re.DOTALL)
_NUMERIC = re.compile(r"^[+-]?\d+(\.\d+)?$")
_ELLIPSIS = re.compile(r"(\.{2,}|…)\s*$")

# Ordered most specific first: `%m/%d/%y` would happily parse `01/02/2026` as year
# 20, so four-digit patterns have to win.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%d/%m/%Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%m/%d/%y",
    "%m-%d-%y",
)


def casefold(s: str) -> str:
    return s.casefold()


def collapse_ws(s: str) -> str:
    """Runs of whitespace to a single space, trimmed.

    OCR line boxes routinely include column padding as spaces.
    """
    return _WS.sub(" ", s).strip()


def strip_currency(s: str) -> str:
    """`$1,234.56` -> `1234.56`. Handles leading symbols, thousands separators and
    parenthesized negatives, which US financial UIs use for debits."""
    t = s.strip()

    # `($441.56)` is minus four hundred and forty-one dollars. Only treat the
    # parentheses as a sign when what they wrap is actually a number — otherwise
    # "(see reverse)" would normalize to "-see reverse".
    negative = False
    wrapped = _PARENTHESIZED.match(t)
    if wrapped:
        inner = _THOUSANDS.sub("", _CURRENCY_SYMBOL.sub("", wrapped.group(1))).strip()
        if _NUMERIC.match(inner):
            negative = True
            t = wrapped.group(1)

    t = _THOUSANDS.sub("", _CURRENCY_SYMBOL.sub("", t)).strip()
    if negative and not t.startswith("-"):
        t = f"-{t}"
    return t


def strip_punct(s: str) -> str:
    """Drop punctuation, keeping letters, digits and single spaces.

    Blunt by design: it exists for comparing labels ("Member ID:" vs "Member ID"),
    not values. Applying it to money deletes the decimal point, which is why
    `strip_currency` is declared before it in every artifact that uses both.
    """
    return collapse_ws(
        "".join(" " if unicodedata.category(c).startswith("P") else c for c in s)
    )


def strip_ellipsis(s: str) -> str:
    """Drops a trailing `...` or `…`.

    Truncated cell text is the single most common reason a correct predicate fails
    to match. Note the asymmetry this implies: after stripping, a truncated value
    can only ever be compared by prefix, never by equality — `cell_equals` against
    a truncated cell is unanswerable and must not silently return False.
    """
    return _ELLIPSIS.sub("", s).rstrip()


def digits_only(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def date_iso(s: str) -> str:
    """Best-effort date normalization to `YYYY-MM-DD`.

    US back-office apps mix `MM/DD/YYYY`, `MM-DD-YY` and `Mon D, YYYY`, often on
    the same screen. Returns the input unchanged when it cannot parse — a
    normalizer must not invent data.
    """
    t = collapse_ws(s)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            continue
    return s


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
    for n in normalizers:
        s = _FUNCS[n](s)
    return s
