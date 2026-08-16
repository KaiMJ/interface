"""Evidence.

One directory per run, written incrementally as the run proceeds rather than
assembled at the end. A run that crashes at step 9 is exactly the run whose
evidence matters most, so evidence that only exists on the success path is
evidence that is missing when it is needed.

    evidence/<run_id>/
      run.json              DiscoveryResult | ReplayResult, rewritten each step
      steps.jsonl           one structured record per step, append-only
      frames/
        step-03.png             what the screen looked like
        step-03.annotated.png   with the set-of-marks overlay (discovery only)
      observations/
        step-03.json          detected elements, for offline debugging
      intervention/
        request.json  handoff.png  handback.png  human_actions.jsonl
      capability.json       the emitted artifact (discovery only)

Everything here passes through the redactor before it is written. That is the
whole reason `Redactor` exists as a seam even though v1's implementation is a
no-op — the call sites are the expensive part to retrofit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class EvidenceWriter:
    def __init__(self, root: Path, run_id: str, redactor: Any) -> None:
        self.dir = root / run_id
        self.run_id = run_id
        self.redactor = redactor

    def open(self) -> Path:
        raise NotImplementedError

    def frame(self, obs: Any, step_id: int, annotated: Path | None = None) -> dict[str, str]:
        """Persist a screenshot (+ optional overlay) and the observation JSON."""
        raise NotImplementedError

    def step(self, result: Any) -> None:
        """Append one StepResult to steps.jsonl."""
        raise NotImplementedError

    def result(self, result: Any) -> None:
        """Rewrite run.json. Called after every step, not only at the end."""
        raise NotImplementedError

    def capability(self, cap: Any) -> Path:
        raise NotImplementedError

    def intervention(self, req: Any, resolution: Any | None = None) -> None:
        raise NotImplementedError
