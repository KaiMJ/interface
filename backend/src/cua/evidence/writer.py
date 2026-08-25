"""Evidence.

One directory per run, written as the run proceeds rather than assembled at the end: a
run that crashes at step 9 is the run whose evidence matters most, so evidence that
exists only on the success path is missing exactly when it is needed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..schema import Evidence


class EvidenceWriter:
    def __init__(self, root: Path, run_id: str, redactor: Any) -> None:
        self.dir = root / run_id
        self.run_id = run_id
        self.redactor = redactor

    def open(self) -> Path:
        for sub in ("frames", "observations", "intervention"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        return self.dir

    def frame(
        self,
        obs: Any,
        step_id: int,
        annotated: Path | None = None,
        after: bool = False,
        prior: Evidence | None = None,
    ) -> Evidence:
        """Persist a screenshot (+ optional overlay) and the observation JSON.

        Copied out of the working directory rather than referenced in place: the
        perceiver reuses one path per run for the live frame, so a reference would
        point at whatever the screen looked like several steps later. Evidence
        that changes after the fact is not evidence.

        `after=True` writes the post-action frame under its own name instead of
        overwriting the one the step acted on. Those are different pictures with
        different meanings — the target was resolved against the first and the
        checkpoint evaluated against the second — and writing both to one path
        loses whichever the step cared about.
        """
        self.open()
        name = f"step-{step_id:02d}" + (".after" if after else "")
        shot = self.dir / "frames" / f"{name}.png"
        source = Path(obs.screenshot_path)
        if source.exists():
            # The redactor is a seam here: v1 returns the frame untouched, and
            # this is the call site that would mask it.
            shutil.copyfile(self.redactor.redact_image(source, shot, obs), shot)

        overlay: Path | None = None
        if annotated is not None and annotated.exists():
            overlay = self.dir / "frames" / f"{name}.annotated.png"
            shutil.copyfile(annotated, overlay)

        observation = self.dir / "observations" / f"{name}.json"
        observation.write_text(obs.model_dump_json(indent=2))

        if after:
            # The acted-on frame is the step's identity; the after-frame is added
            # to it rather than replacing it.
            base = prior or Evidence()
            return base.model_copy(update={"after": str(shot) if source.exists() else None})
        return Evidence(
            screenshot=str(shot) if source.exists() else None,
            annotated_screenshot=str(overlay) if overlay else None,
            observation=str(observation),
        )

    def step(self, result: Any) -> None:
        """Append one StepResult to steps.jsonl."""
        self.open()
        with (self.dir / "steps.jsonl").open("a") as f:
            f.write(result.model_dump_json() + "\n")

    def result(self, result: Any) -> None:
        """Rewrite run.json. Called after every step, not only at the end.

        A run that dies at step 9 is exactly the run whose evidence matters most,
        so the file has to be current *before* the step that might not return.
        """
        self.open()
        (self.dir / "run.json").write_text(result.model_dump_json(indent=2))

    def capability(self, cap: Any) -> Path:
        self.open()
        path = self.dir / "capability.json"
        path.write_text(cap.model_dump_json(indent=2, exclude_none=True))
        return path

    def intervention(self, req: Any, resolution: Any | None = None) -> None:
        self.open()
        (self.dir / "intervention" / "request.json").write_text(req.model_dump_json(indent=2))
        if resolution is not None:
            (self.dir / "intervention" / "resolution.json").write_text(
                resolution.model_dump_json(indent=2)
            )
            # The operator's own actions, one per line, in the order they made
            # them. Captured at the X layer — see escalation/watch.py.
            with (self.dir / "intervention" / "human_actions.jsonl").open("w") as f:
                for action in resolution.human_actions:
                    f.write(action.model_dump_json() + "\n")

    def note(self, name: str, payload: dict[str, Any]) -> Path:
        """Anything else worth keeping — the model transcript, a policy denial.

        Deliberately generic: the alternative is a method per artifact type and a
        writer edited every time a run learns to record something new.
        """
        self.open()
        path = self.dir / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, default=str))
        return path
