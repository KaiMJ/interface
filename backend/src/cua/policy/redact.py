"""Redaction — the seam, stubbed.

The tension is specific to a vision-first design: a screenshot is simultaneously the
evidence *and* the model input, and a bank screen is PII by construction. Declared
sensitive values are redacted for real; pattern masking is wired at the call sites but
paints nothing onto a frame, and the call sites existing is the part that matters,
because retrofitting one into code that already writes screenshots everywhere is the
expensive version of this problem.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..schema import Observation


class Redactor:
    """Declared redaction is real; pattern-based redaction is a seam."""

    MASK = "[redacted]"

    def __init__(self, patterns: tuple[str, ...] = (), enabled: bool = False) -> None:
        self.patterns = patterns
        self.enabled = enabled
        self._res = tuple(re.compile(p) for p in patterns) if enabled else ()

    def redact_image(self, src: Path, dst: Path, obs: Observation | None = None) -> Path:
        """Mask PII regions in a screenshot.

        Would work by matching `patterns` against OCR elements in `obs` and
        painting over their boxes — which is precisely why `Observation` carries
        text boxes and not just an image. v1 returns the frame untouched.
        """
        return src

    def redact_text(self, s: str) -> str:
        """Mask PII in a log line or a result field."""
        for r in self._res:
            s = r.sub(self.MASK, s)
        return s

    def redact_mapping(
        self, d: dict[str, object], sensitive_keys: frozenset[str]
    ) -> dict[str, object]:
        """Replace declared-sensitive input values before a result is serialized.

        Distinct from pattern matching and strictly stronger: `InputSpec.sensitive`
        is a declaration, not a guess, so it cannot miss. This one is implemented.
        """
        return {k: (self.MASK if k in sensitive_keys else v) for k, v in d.items()}
