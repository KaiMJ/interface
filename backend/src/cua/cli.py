"""CLI — the demo path.

The README's four commands map to the four subcommands here. A reviewer should be
able to run the whole thread without touching HTTP:

    cua discover  --goal "..." --input member_id=12345
    cua replay    cap_get_savings_balance --input member_id=12345
    cua replay    cap_get_savings_balance --input member_id=99999   # business outcome
    cua catalog

`replay` runs with no API key set. That is the check that matters: if the
deterministic path needs a model, it is not deterministic.
"""

from __future__ import annotations

from typing import Annotated

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _parse_inputs(pairs: list[str]) -> dict[str, str]:
    """`--input k=v` repeated -> dict."""
    raise NotImplementedError


@app.command()
def discover(
    goal: Annotated[str, typer.Option(help="Natural-language goal")],
    start_url: Annotated[str, typer.Option(help="Entry point")] = "",
    input: Annotated[list[str] | None, typer.Option(help="key=value, repeatable")] = None,
    capability_id: Annotated[str, typer.Option(help="Id for the emitted artifact")] = "",
) -> None:
    """LLM-driven run against the live surface; emits a draft capability."""
    raise NotImplementedError


@app.command()
def replay(
    capability_id: Annotated[str, typer.Argument()],
    input: Annotated[list[str] | None, typer.Option(help="key=value, repeatable")] = None,
    version: Annotated[int, typer.Option()] = 0,
) -> None:
    """Deterministic replay. Prints a ReplayResult and exits non-zero on failure.

    Exit codes distinguish the classes, so a shell caller sees the taxonomy too:
      0  success
      0  business outcome        (an answer, not an error)
      2  escalated
      1  hard failure
    """
    raise NotImplementedError


@app.command()
def catalog(status: Annotated[str, typer.Option()] = "") -> None:
    """List saved capabilities with their inputs, outputs and outcomes."""
    raise NotImplementedError


@app.command()
def approve(capability_id: str, version: int, operator: str = "reviewer") -> None:
    """draft -> approved."""
    raise NotImplementedError


if __name__ == "__main__":
    app()
