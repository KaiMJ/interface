"""The capability catalog.

Files on disk: `artifacts/<capability_id>.v<n>.json`. No database. The catalog is
read on every invocation and written once per discovery run; an index would be
infrastructure in search of a problem at this size, and the brief is explicit that
building scaling plumbing is not rewarded.

Filename versioning rather than an in-file-only version, because it means old
versions are retained by construction and a diff between v2 and v3 is `git diff`
rather than a feature we have to build.

This module is also the natural home for the stretch-goal capability interface:
`list()` plus `Capability.inputs`/`outputs` is already a tool catalog an agent
could discover and call by name.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..schema import Capability, Status


class CapabilityNotFound(KeyError):
    pass


class Catalog:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, cap: Capability) -> Path:
        """Write a new version. Never overwrites an existing (id, version).

        A discovery run that re-records an existing capability produces v(n+1) in
        `draft`, leaving the approved version in place. Re-recording must not be
        able to silently replace what production is calling.
        """
        raise NotImplementedError

    def load(self, capability_id: str, version: int | None = None) -> Capability:
        """Latest version when `version` is None."""
        raise NotImplementedError

    def list(self, status: Status | None = None) -> Sequence[Capability]:
        # Sequence, not list: a catalog listing is read-only, and naming a method
        # `list` shadows the builtin inside this class body -- so the annotation
        # has to avoid it anyway.
        raise NotImplementedError

    def approve(self, capability_id: str, version: int, approver: str) -> Capability:
        """draft -> approved.

        The gate on unattended replay. Synthesis proposes outputs, checkpoints and
        business outcomes with a model's help, and that is exactly the part that
        should not go to production unreviewed. Putting the human here is more
        honest than pretending synthesis is reliable.
        """
        raise NotImplementedError

    def tool_manifest(self) -> Sequence[dict[str, object]]:
        """Approved capabilities as function-calling tool definitions.

        The agent-facing surface: name, description, JSON-schema inputs derived
        from `InputSpec`, and the declared output shape plus business outcomes so
        a calling agent knows every result it can receive.
        """
        raise NotImplementedError
