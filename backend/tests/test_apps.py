"""One app per policy file, selected per run.

The check these tests exist for: adding a second application must be adding a
YAML file, not editing `backend/src`. Anything here that needed a Python change
would be a bug in the architecture rather than a configuration step, so the
second app below is written from scratch in a temp directory and never imported.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cua.config import Settings
from cua.policy import Policy, PolicyDenied
from cua.runtime import build_policy, entry_url
from cua.runtime.session import SessionPool, WrongApp

REPO = Path(__file__).resolve().parents[2]

SECOND_APP = """
app: coreview
vendor: fiserv
base_url_pattern: "^https://coreview\\\\.[a-z0-9-]+\\\\.example(/.*)?$"
entry_url: https://coreview.riverside.example
allowed_url_patterns:
  - "^https://coreview\\\\.[a-z0-9-]+\\\\.example(/.*)?$"
allowed_actions: [navigate, click, extract, assert]
risky_disposition: block
app_errors:
  - name: core_unavailable
    detector: { kind: text_present, value: "CORE-500" }
surface: a core banking terminal
"""


@pytest.fixture
def two_apps(tmp_path: Path) -> Settings:
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "targetapp.yaml").write_text(
        (REPO / "policies" / "targetapp.yaml").read_text()
    )
    (policies / "coreview.yaml").write_text(SECOND_APP)
    return Settings(policies_dir=policies, target_base_url="")


def test_two_applications_coexist(two_apps: Settings) -> None:
    assert two_apps.apps() == ["coreview", "targetapp"]

    first = build_policy(two_apps, "targetapp")
    second = build_policy(two_apps, "coreview")

    assert first.app == "targetapp"
    assert second.app == "coreview"
    # Not the same guardrails, and neither one leaks into the other.
    assert second.risky_disposition == "block"
    assert first.risky_disposition == "confirm"
    with pytest.raises(PolicyDenied):
        second.check_url("http://targetapp:8080/members/12345")
    with pytest.raises(PolicyDenied):
        first.check_url("https://coreview.riverside.example/accounts")


def test_an_unknown_app_names_the_ones_that_exist(two_apps: Settings) -> None:
    """Rather than silently falling back to a default policy, which would run one
    application under another's allowlist."""
    with pytest.raises(FileNotFoundError, match="coreview"):
        two_apps.policy_path("nosuchapp")


def test_a_capability_carries_the_app_it_was_recorded_against(two_apps: Settings) -> None:
    """`AppRef` comes from the policy, so an artifact cannot name an application
    that has no guardrails."""
    ref = build_policy(two_apps, "coreview").app_ref()

    assert ref.name == "coreview"
    assert ref.vendor == "fiserv"
    # A pattern, not a literal: the same artifact is valid against another
    # institution's install of the same vendor product.
    import re

    assert re.match(ref.base_url_pattern, "https://coreview.lakeside.example/accounts")
    assert re.match(ref.base_url_pattern, "https://coreview.riverside.example/accounts")


def test_the_entry_url_is_per_deployment_and_the_policy_is_not(two_apps: Settings) -> None:
    """The multi-tenant seam, in one assertion: a second institution running the
    same vendor product overrides the URL and shares everything else."""
    policy = build_policy(two_apps, "coreview")
    assert entry_url(two_apps, policy) == "https://coreview.riverside.example"

    lakeside = two_apps.model_copy(
        update={"target_base_url": "https://coreview.lakeside.example"}
    )
    assert entry_url(lakeside, policy) == "https://coreview.lakeside.example"
    # Same policy object. Nothing about the guardrails changed with the tenant.
    assert build_policy(lakeside, "coreview").app_ref() == policy.app_ref()


async def test_one_display_cannot_hold_two_applications(tmp_path: Path) -> None:
    """The X display is the coordinate space, so a second app's browser on it
    would put two applications in one picture."""

    class FakeSession:
        def __init__(self, app: str | None) -> None:
            self.app = app

        async def start(self) -> None:
            return None

    pool = SessionPool(factory=lambda _display, app: FakeSession(app))  # type: ignore[arg-type]

    first = await pool.acquire(":1", "targetapp")
    assert await pool.acquire(":1", "targetapp") is first

    with pytest.raises(WrongApp, match="coreview"):
        await pool.acquire(":1", "coreview")


def test_the_shipped_policy_declares_an_identity() -> None:
    """The repo's own policy has to satisfy what the code now reads from it."""
    policy = Policy.load(REPO / "policies" / "targetapp.yaml")
    assert policy.app == "targetapp"
    assert policy.base_url_pattern
    assert policy.entry_url
