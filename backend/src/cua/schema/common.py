"""Primitives shared by artifacts, observations, and results.

Coordinates are normalized to 0..1 of the recording viewport, and the viewport is
recorded alongside them. Absolute pixels would make an artifact unreadable to a
human reviewer and unusable at any other geometry; normalized coordinates plus a
declared viewport keep both properties.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class Frozen(BaseModel):
    """Base for value types. Artifacts are read many times and mutated never."""

    model_config = ConfigDict(frozen=True, extra="forbid")


Unit = Annotated[float, Field(ge=0.0, le=1.0)]


class Point(Frozen):
    x: Unit
    y: Unit


class Bbox(Frozen):
    """Normalized box. (x, y) is the top-left corner."""

    x: Unit
    y: Unit
    w: Unit
    h: Unit

    @property
    def center(self) -> Point:
        return Point(x=self.x + self.w / 2, y=self.y + self.h / 2)

    def contains(self, p: Point) -> bool:
        return self.x <= p.x <= self.x + self.w and self.y <= p.y <= self.y + self.h

    def contained_by(self, other: Bbox) -> float:
        """Fraction of *this* box that lies inside `other`. 1.0 means enclosed.

        Distinct from `iou` on purpose: a text line inside a large button has a
        tiny IoU with it and a containment of 1.0, and it is containment that
        decides whether the line is that button's label.
        """
        ix = max(0.0, min(self.x + self.w, other.x + other.w) - max(self.x, other.x))
        iy = max(0.0, min(self.y + self.h, other.y + other.h) - max(self.y, other.y))
        area = self.w * self.h
        return (ix * iy) / area if area > 0 else 0.0

    def iou(self, other: Bbox) -> float:
        ix = max(0.0, min(self.x + self.w, other.x + other.w) - max(self.x, other.x))
        iy = max(0.0, min(self.y + self.h, other.y + other.h) - max(self.y, other.y))
        inter = ix * iy
        union = self.w * self.h + other.w * other.h - inter
        return inter / union if union > 0 else 0.0


class Viewport(Frozen):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class ValueType(str, Enum):
    """Types an artifact's inputs and outputs may declare.

    Deliberately small. There is no `secret_ref` type: secrets are resolved in the
    action layer from the environment and never travel through a typed field that
    something might serialize.
    """

    STRING = "string"
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    DATE = "date"
    ENUM = "enum"


class Normalizer(str, Enum):
    """Text transforms applied before any comparison.

    Recorded in the artifact rather than hardcoded in the engine, so that a replay
    compares strings exactly the way the recording did. `$1,234.56` vs `1234.56`
    and `ACME Corp...` vs `ACME Corporation` are the everyday cases.
    """

    CASEFOLD = "casefold"
    COLLAPSE_WS = "collapse_ws"
    STRIP_CURRENCY = "strip_currency"
    STRIP_PUNCT = "strip_punct"
    STRIP_ELLIPSIS = "strip_ellipsis"
    DIGITS_ONLY = "digits_only"
    DATE_ISO = "date_iso"


class MatchMode(str, Enum):
    EXACT = "exact"
    CONTAINS = "contains"
    REGEX = "regex"


class Risk(str, Enum):
    """Whether an action can be taken back.

    Classifiable only because every step carries a declared intent. `click(0.42,
    0.71)` cannot be judged reversible or not; "submit the transfer" can.
    """

    SAFE = "safe"          # read-only or trivially reversible: navigate, read, type
    RISKY = "risky"        # mutates state at the institution: submit, confirm, delete


# A string that may contain `{{param}}` placeholders resolved from the caller's
# inputs at replay time. Kept as a plain str so artifacts stay readable as JSON.
Template = str
