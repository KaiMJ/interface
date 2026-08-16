"""Recording what the human did.

§3.6 requires capturing the operator's actions, and this is the part that a
headful-Playwright design cannot do: Playwright observes the events it issues, and
a manual click is not one of them. Instrumenting the page with JS listeners would
half-work — it would miss anything outside the page, it breaks on a surface with
no DOM, and it puts the audit trail inside the thing being audited.

Capturing at the X layer instead means the same code records a human operating a
browser and a human operating a desktop application, and there is no hole in the
audit trail exactly where a person touched regulated data.

Implementation: an XRecord/XInput2 tap on the display for the duration of the
intervention, emitting `HumanAction` records. Typed text is captured as a keystroke
*count* and never as content — the operator may be entering a credential, and an
audit log that records what someone typed into a password field is a worse
liability than one that does not.

Also captures a screenshot at handoff and at handback, so the run's evidence shows
what the operator was given and what they left behind.
"""

from __future__ import annotations

from pathlib import Path

from ..schema import HumanAction


class HumanActionWatcher:
    """Records operator input for the life of one intervention."""

    def __init__(self, display: str) -> None:
        self.display = display
        self._actions: list[HumanAction] = []
        self._running = False

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> list[HumanAction]:
        raise NotImplementedError

    def snapshot(self, out_path: Path, label: str) -> Path:
        """Evidence frame at handoff / handback."""
        raise NotImplementedError
