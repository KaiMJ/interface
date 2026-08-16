"""Set-of-Marks overlay — the discovery-time view.

The model is shown the screenshot with numbered boxes drawn over every candidate,
plus a compact JSON list of those candidates. It replies with a mark id, not with
coordinates.

This is the single decision that makes discovery recordings replayable by
construction. If the model returned pixel coordinates we would have to infer, after
the fact, *what* it meant to click — and that inference is exactly the fragile
post-hoc step this design is trying to avoid. By making it choose from an
enumerated set, the chosen element's role, name, text and box are known exactly,
and the artifact's `Target` can be written from real data rather than guessed.

Replay does not use this module at all. Replay has a `Target` and needs a
coordinate; that is `resolve/`.
"""

from __future__ import annotations

from pathlib import Path

from ..schema import Element, Observation


def annotate(obs: Observation, out_path: Path) -> Path:
    """Draw numbered boxes over the screenshot; return the annotated image path.

    Both images are kept in evidence. The annotated one is what the model saw and
    therefore what any argument about a bad decision has to be litigated against;
    the clean one is what the operator sees.
    """
    raise NotImplementedError


def candidate_digest(elements: tuple[Element, ...], max_items: int = 80) -> list[dict[str, object]]:
    """Compact the element list for the prompt.

    Truncated on purpose. A dense enterprise screen can produce several hundred
    boxes; sending all of them costs tokens and, more importantly, degrades the
    model's ability to pick correctly. Ordering is reading order, so truncation
    drops the bottom of the page rather than an arbitrary slice — and when it
    truncates, the loop is told, so scrolling remains available to it.
    """
    raise NotImplementedError
