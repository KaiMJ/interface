"""The `find_and_act` scan loop.

Fully deterministic: locate the scope, observe it, evaluate the predicate over
detected rows, advance, repeat. No model in it.

    locate scope (by anchor text, not a fixed box)
      └─ loop, bounded by scan.max_advances:
           observe scope region
           group into rows, normalize, test predicate
           match?      -> act / collect
           no match?   -> advance (scroll or click "Next"), re-observe
           stopped changing? -> exhausted

Termination is the part that has to be right, because getting it wrong produces
the specific mistake the brief singles out — confusing "the record is not there"
with "we stopped looking".

  exhausted the list, no match
      -> BUSINESS OUTCOME (`on_not_found_outcome`). A legitimate answer.

  hit max_advances while the region was still changing
      -> HARD FAILURE (SCAN_INCONCLUSIVE). We do not know whether the record is
         absent or we quit early. Reporting "not found" here would be a
         confidently wrong answer, which is worse than an error.

Two other rules that decide whether this works at all:

  - Advance by ~(1 - overlap) of the region height, never a full height. A row
    straddling the boundary would otherwise be skipped and reported as a false
    not-found — the same wrong answer, arrived at more subtly.
  - Ambiguity is first-class. Two matches on a read may be tolerable; on a write,
    acting on the wrong record is unrecoverable, so `on_multiple=escalate` is the
    default and the caller must opt out deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..schema import Bbox, Element, FindAndActStep, Observation


@dataclass(frozen=True)
class ScanResult:
    matches: list[list[Element]]         # each match is a row (list of elements)
    advances: int
    exhausted: bool                      # region stopped changing -> we saw everything
    inconclusive: bool                   # hit max_advances with content still moving


class Scanner:
    def __init__(self, perceiver: Any, driver: Any, resolver: Any) -> None:
        self.perceiver = perceiver
        self.driver = driver
        self.resolver = resolver

    def locate_scope(self, step: FindAndActStep, obs: Observation, params: dict[str, Any]) -> Bbox:
        """Resolve the scope anchor and derive the region from `scope_extent`.

        Anchor-based rather than a recorded box so that a banner appearing above
        the table does not silently shift the scan window off the data.
        """
        raise NotImplementedError

    def scan(self, step: FindAndActStep, params: dict[str, Any]) -> ScanResult:
        raise NotImplementedError

    def _test(self, row: list[Element], step: FindAndActStep, params: dict[str, Any]) -> bool:
        """Evaluate the predicate against one row, with the artifact's normalizers.

        Note the truncation asymmetry: after `strip_ellipsis`, a truncated cell can
        only be compared by prefix. `cell_equals` against a value we know was
        truncated is unanswerable, and must raise rather than quietly return False.
        """
        raise NotImplementedError
