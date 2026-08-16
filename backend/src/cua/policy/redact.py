"""Redaction — the seam, stubbed.

The tension is specific to a vision-first design and worth naming rather than
hiding: a screenshot is simultaneously the evidence *and* the model input, and a
bank screen is PII by construction. A DOM-based system can redact fields it knows
about; we have pixels.

Three places redaction could sit, and they are not equivalent:

  a. before evidence is written   — protects the artifact repo and the log store
  b. before the LLM sees a frame  — protects the model provider
  c. before either               — both

(b) is in tension with the task: an agent asked to read a savings balance cannot
do it if the balance is masked. A real deployment resolves this with a
zero-retention / BAA agreement with the provider rather than by masking, and says
so out loud.

Decision for v1: the seam is built and wired at both call sites; the
implementation is a no-op. Documented as a cut in REPORT §7 rather than left as an
implied capability. The call sites existing is the part that matters — retrofitting
a redaction point into code that already writes screenshots everywhere is the
expensive version of this problem.
"""

from __future__ import annotations

from pathlib import Path

from ..schema import Observation


class Redactor:
    """No-op in v1. Both call sites are real; the masking is not."""

    def __init__(self, patterns: tuple[str, ...] = (), enabled: bool = False) -> None:
        self.patterns = patterns
        self.enabled = enabled

    def redact_image(self, src: Path, dst: Path, obs: Observation | None = None) -> Path:
        """Mask PII regions in a screenshot.

        Would work by matching `patterns` against OCR elements in `obs` and
        painting over their boxes — which is precisely why `Observation` carries
        text boxes and not just an image.
        """
        raise NotImplementedError

    def redact_text(self, s: str) -> str:
        """Mask PII in a log line or a result field."""
        raise NotImplementedError

    def redact_mapping(
        self, d: dict[str, object], sensitive_keys: frozenset[str]
    ) -> dict[str, object]:
        """Replace declared-sensitive input values before a result is serialized.

        Distinct from pattern matching and strictly stronger: `InputSpec.sensitive`
        is a declaration, not a guess, so it cannot miss.
        """
        raise NotImplementedError
