"""`{{param}}` substitution.

The one place a caller's inputs enter a recorded string. Anchors, checkpoint
values and predicate terms are all templates, which is what makes a recording of
"the row for member 12345" replayable as "the row for member 90001" without the
model.

Two rules, both deliberate:

  - An unknown placeholder raises. Rendering `{{member_id}}` to the empty string
    would turn "find the row containing 12345" into "find the row containing
    nothing", which matches the first row on the page. A missing parameter is a
    caller error and has to surface as one.
  - Substituted values are never re-scanned for placeholders. A member's name
    containing `{{` is data, not a template.
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

    Longest value first, so an input of `123` does not corrupt a recorded `12345`
    belonging to a different input. This is how a recording becomes reusable —
    "No member record found for ID 99999" is a fact about one run, and "... for ID
    {{member_id}}" is a fact about the capability.
    """
    if text is None:
        return None
    for name, value in sorted(
        ((n, str(v)) for n, v in (params or {}).items() if str(v)),
        key=lambda pair: len(pair[1]),
        reverse=True,
    ):
        text = text.replace(value, f"{{{{{name}}}}}")
    return text


def placeholders(template: str | None) -> set[str]:
    """Which parameters a template needs. Used to validate an invocation before
    it starts touching the application, rather than failing at step 7."""
    return set() if template is None else {m.group(1) for m in _PLACEHOLDER.finditer(template)}
