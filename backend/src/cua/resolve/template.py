"""`{{param}}` substitution: the one place a caller's inputs enter a recorded string, and what
makes "the row for member 12345" replayable as "the row for member 90001" with no model.

An unknown placeholder raises. Rendering it to the empty string turns "find the row containing
12345" into "find the row containing nothing".
"""

from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class MissingParam(KeyError):
    """A template referenced a parameter the caller did not supply."""

    def __init__(self, name: str, template: str) -> None:
        super().__init__(name)
        self.name = name
        self.template = template

    def __str__(self) -> str:
        return f"template {self.template!r} needs parameter {self.name!r}"


def render(template: str | None, params: dict[str, Any] | None = None) -> str | None:
    """Substitute `{{name}}` from `params`. `None` in, `None` out."""
    if template is None:
        return None
    values = params or {}

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in values:
            raise MissingParam(name, template)
        return str(values[name])

    return _PLACEHOLDER.sub(sub, template)


def unrender(text: str | None, params: dict[str, Any] | None = None) -> str | None:
    """The inverse of `render`: replace known values with their placeholders.

    How a recording becomes reusable. Two rules keep it from parameterizing what the caller
    never meant: **longest value first**, so an input of `123` does not eat the leading digits
    of a recorded `12345`; and **only at token boundaries**, since a bare `str.replace` turns
    the account number `9912345` into `99{{member_id}}`.

    It does not decide whether a correctly bounded match was *meant*: if a member id and a
    branch code are both `12345` on the recording run, only a second run could tell them apart.
    """
    if text is None:
        return None
    for name, value in sorted(
        ((n, str(v)) for n, v in (params or {}).items() if str(v)),
        key=lambda pair: len(pair[1]),
        reverse=True,
    ):
        text = re.sub(
            rf"(?<![0-9A-Za-z_]){re.escape(value)}(?![0-9A-Za-z_])",
            f"{{{{{name}}}}}",
            text,
        )
    return text


def placeholders(template: str | None) -> set[str]:
    """Which parameters a template needs. Validates an invocation before it starts touching the
    application, rather than failing at step 7."""
    return set() if template is None else {m.group(1) for m in _PLACEHOLDER.finditer(template)}
