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

import json
import re
from collections.abc import Sequence
from pathlib import Path

from ..schema import Capability, InputSpec, Status, ValueType

_FILENAME = re.compile(r"^(?P<id>.+)\.v(?P<version>\d+)\.json$")

# ValueType -> JSON Schema, for the tool manifest. Small and explicit rather than
# generated, because this is a published contract: a silent change in how a type
# is exposed would change what calling agents send us.
_JSON_TYPES: dict[ValueType, str] = {
    ValueType.STRING: "string",
    ValueType.NUMBER: "number",
    ValueType.INTEGER: "integer",
    ValueType.BOOLEAN: "boolean",
    ValueType.DATE: "string",
    ValueType.ENUM: "string",
}


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
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(cap.id, cap.version)
        if path.exists():
            raise FileExistsError(
                f"{cap.ref} already exists; record a new version rather than "
                f"replacing what production may be calling"
            )
        path.write_text(cap.model_dump_json(indent=2, exclude_none=True))
        return path

    def load(self, capability_id: str, version: int | None = None) -> Capability:
        """Latest version when `version` is None."""
        if version is None:
            versions = self.versions(capability_id)
            if not versions:
                raise CapabilityNotFound(capability_id)
            version = versions[-1]
        path = self._path(capability_id, version)
        if not path.exists():
            raise CapabilityNotFound(f"{capability_id}@v{version}")
        return Capability.model_validate_json(path.read_text())

    def versions(self, capability_id: str) -> Sequence[int]:
        return sorted(
            int(m.group("version"))
            for m in (_FILENAME.match(p.name) for p in self._files())
            if m and m.group("id") == capability_id
        )

    def list(
        self, status: Status | None = None, app: str | None = None
    ) -> Sequence[Capability]:
        # Sequence, not list: a catalog listing is read-only, and naming a method
        # `list` shadows the builtin inside this class body -- so the annotation
        # has to avoid it anyway.
        caps = [Capability.model_validate_json(p.read_text()) for p in self._files()]
        caps.sort(key=lambda c: (c.app.name, c.id, c.version))
        return [
            c
            for c in caps
            if (status is None or c.status is status) and (app is None or c.app.name == app)
        ]

    def approve(self, capability_id: str, version: int, approver: str) -> Capability:
        """draft -> approved.

        The gate on unattended replay. Synthesis proposes outputs, checkpoints and
        business outcomes with a model's help, and that is exactly the part that
        should not go to production unreviewed. Putting the human here is more
        honest than pretending synthesis is reliable.
        """
        cap = self.load(capability_id, version)
        approved = cap.model_copy(update={"status": Status.APPROVED})
        # Rewriting in place is the one legitimate overwrite: the flow did not
        # change, a human vouched for it. The approver is recorded in the run log,
        # not in the artifact, so the artifact stays a description of the flow.
        self._path(capability_id, version).write_text(
            approved.model_dump_json(indent=2, exclude_none=True)
        )
        return approved

    def tool_manifest(self, app: str | None = None) -> Sequence[dict[str, object]]:
        """Approved capabilities as function-calling tool definitions.

        The agent-facing surface: name, description, JSON-schema inputs derived
        from `InputSpec`, and the declared output shape plus business outcomes so
        a calling agent knows every result it can receive.

        Only approved capabilities appear. A draft is a proposal a human has not
        vouched for, and an agent that could call one would be running unreviewed
        automation against member accounts.
        """
        return [
            {
                "name": cap.id,
                "description": cap.description or cap.goal,
                "version": cap.version,
                # Which application this drives. A calling agent holding tools for
                # several back-office systems needs it to disambiguate; it is also
                # the only field here that says where the guardrails came from.
                "app": cap.app.name,
                "parameters": {
                    "type": "object",
                    "properties": {i.name: _json_schema(i) for i in cap.inputs},
                    "required": [i.name for i in cap.inputs if i.required],
                },
                "returns": {
                    "type": "object",
                    "properties": {
                        o.name: {
                            "type": _JSON_TYPES[o.type],
                            "description": o.description,
                        }
                        for o in cap.outputs
                    },
                },
                # Declared alternatives, so a calling agent can branch on "no such
                # member" instead of treating it as a failed call.
                "outcomes": [
                    {"name": o.name, "description": o.description}
                    for o in cap.business_outcomes
                ],
            }
            for cap in self.list(status=Status.APPROVED, app=app)
        ]

    # --- internals -----------------------------------------------------------

    def _path(self, capability_id: str, version: int) -> Path:
        return self.root / f"{capability_id}.v{version}.json"

    def _files(self) -> Sequence[Path]:
        return sorted(self.root.glob("*.v*.json")) if self.root.exists() else []


def _json_schema(spec: InputSpec) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": _JSON_TYPES[spec.type],
        "description": spec.description,
    }
    if spec.type is ValueType.DATE:
        schema["format"] = "date"
    if spec.example:
        schema["examples"] = [spec.example]
    if spec.constraints:
        c = spec.constraints
        if c.pattern:
            schema["pattern"] = c.pattern
        if c.min is not None:
            schema["minimum"] = c.min
        if c.max is not None:
            schema["maximum"] = c.max
        if c.choices:
            schema["enum"] = list(c.choices)
    return schema


def write_manifest(catalog: Catalog, path: Path) -> Path:
    """Dump the manifest to disk — the stretch-goal agent-facing surface, as a
    file an agent framework can load without running this service."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(catalog.tool_manifest()), indent=2))
    return path
