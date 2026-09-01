"""CLI-level helpers that are not reachable through the engine."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import typer


def test_offline_frames_are_the_screens_the_run_saw_each_appearing_once() -> None:
    """`step-04.after.png` is byte-identical to `step-05.png` — it is the same screen.

    `ImageFileScreen` advances only when the driver acts, so the feed is one frame per
    transition. Passing both spellings feeds every screen twice and the run ends up a frame
    behind.
    """
    from cua.cli import _observation_sequence

    names = [
        "step-02.after.png",
        "step-01.png",
        "step-10.png",
        "step-02.png",
        "step-01.after.png",
        "step-10.after.png",
    ]
    ordered = [p.name for p in _observation_sequence(Path(n) for n in names)]

    assert ordered == [
        "step-01.png",
        "step-02.png",
        "step-10.png",
        "step-10.after.png",
    ]


def test_a_chosen_run_id_is_prefixed_checked_and_never_clobbers(tmp_path: Path) -> None:
    """A run id becomes a directory name, so it is validated rather than trusted."""
    import cua.cli as cli

    with mock.patch.object(cli, "settings", lambda: SimpleNamespace(evidence_dir=tmp_path)):
        assert cli._run_id("banner", "replay") == "replay-banner"
        assert cli._run_id("replay-banner", "replay") == "replay-banner"
        assert cli._run_id("", "replay").startswith("replay-")

        for bad in ["../escape", "Has Caps", "trailing/slash", ""]:
            if not bad:
                continue
            with pytest.raises(typer.BadParameter):
                cli._run_id(bad, "replay")

        (tmp_path / "replay-taken").mkdir()
        with pytest.raises(typer.BadParameter):
            cli._run_id("taken", "replay")
