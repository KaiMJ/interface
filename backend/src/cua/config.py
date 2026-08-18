"""Runtime configuration. One place, environment-driven, no scattered getenv."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .schema import Viewport

# The container mounts everything under /app and /data. Outside it — `make api`,
# `make test`, a reviewer running the CLI from a checkout — those paths do not
# exist and are not creatable, so each default falls back to the same file in the
# repository. Defaults that only work in one of the two places are defaults that
# send people to the configuration reference to run the demo.
_REPO = Path(__file__).resolve().parents[3]


def _path(container: str, in_repo: str) -> Path:
    installed = Path(container)
    return installed if installed.exists() else _REPO / in_repo


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUA_", env_file=".env", extra="ignore")

    # --- LLM (discovery only; replay must run with all of this unset) --------
    #
    # Routed through LiteLLM, so `model` is a provider-qualified string and the
    # credential is whichever env var that provider expects (XAI_API_KEY,
    # ANTHROPIC_API_KEY, OPENAI_API_KEY, ...). LiteLLM reads those directly; we
    # deliberately do not copy them into this object, because a key that lives in
    # a pydantic model is a key that can end up in a serialized settings dump.
    #
    # The model must support vision AND tool calling — the loop is built on
    # screenshots in and one tool call out. Anything else fails at the first turn.
    model: str = "xai/grok-4"
    fallback_models: tuple[str, ...] = ()
    api_base: str | None = None          # for a proxy or a self-hosted endpoint
    max_discovery_steps: int = 30
    max_tokens: int = 4096
    temperature: float = 0.0             # discovery is exploratory; sampling is not

    # --- Surface --------------------------------------------------------------
    display: str = Field(default=":1", alias="DISPLAY")
    display_width: int = 1440
    display_height: int = 900
    # Where this deployment's install of the app actually lives. Empty means "use
    # whatever the app's policy declares". The split matters: the *policy* is a
    # fact about the vendor product and is shared, the URL is a fact about one
    # institution's install of it and is not. That is the seam a second tenant
    # would arrive through — a different entry URL against the same policy file.
    target_base_url: str = ""

    # --- Storage --------------------------------------------------------------
    artifacts_dir: Path = Field(default_factory=lambda: _path("/data/artifacts", "artifacts"))
    evidence_dir: Path = Field(default_factory=lambda: _path("/data/evidence", "evidence"))
    models_dir: Path = Field(default_factory=lambda: _path("/models", "models"))
    # One file per application, selected per run by `--app`. A directory rather
    # than a single file because the system is meant to drive more than one
    # application, and "which app" is the first thing every command needs to know.
    #
    # At the repository root, beside `artifacts/` and `evidence/`, because the
    # three of them are the system's data in the order it moves: policies are
    # what a human authors, artifacts are what the system records, evidence is
    # what it did. It is also mounted into the container rather than built into
    # the image — a guardrail that needs a rebuild to change is a guardrail
    # nobody edits.
    policies_dir: Path = Field(
        default_factory=lambda: _path("/app/policies", "policies")
    )
    # Used when a command names no app and the capability being run does not
    # either — i.e. only for `cua discover`, which is the one command that runs
    # before any artifact exists to say which app it belongs to.
    default_app: str = "targetapp"

    # --- Perception -----------------------------------------------------------
    # Detection backend. `omniparser` is the real one; `ocr_only` exists so the
    # replay path and the tests can run without a 2GB torch install.
    detector: str = "omniparser"
    # Fetched from HuggingFace into HF_HOME (under models_dir, bind-mounted) on
    # first use, so the image never carries ~300MB of weights.
    #
    # Note this is the upstream release, not a local checkpoint. OmniParser also
    # publishes the same network as a TorchScript archive, which ultralytics
    # refuses to load and which would mean hand-writing decode + NMS; the release
    # here is a real ultralytics checkpoint, so postprocessing comes for free.
    omniparser_repo: str = "microsoft/OmniParser-v2.0"
    omniparser_repo_file: str = "icon_detect/model.pt"
    detect_conf_threshold: float = 0.30
    merge_iou_threshold: float = 0.60
    # Which backend runs text recognition. `onnxruntime` is the CPU path and the
    # default; `torch` runs the same PP-OCR models through the torch install the
    # detector already uses, which is the only way to reach this machine's GPU —
    # onnxruntime-gpu ships CUDA 12 wheels and torch here is CUDA 13, so its CUDA
    # provider loads and then registers no device.
    #
    # It is a setting rather than a swap because OCR is ~95% of a replay's wall
    # clock (measured: 2.4s of a 2.42s observation on a dense screen), so the
    # backend is the single largest performance decision in the system and should
    # be visible as one. `scripts/bench_perception.py` measures both.
    ocr_engine: str = "onnxruntime"
    # Below this, an OCR line is treated as unreadable rather than as text. Anchor
    # resolution and checkpoints both compare against OCR output, so a confidently
    # wrong string is worse than a missing one.
    ocr_conf_threshold: float = 0.50
    # Longest side PP-OCR's detector sees. Its default downscales a 1440x900
    # display far enough that small coloured text stops being legible: measured on
    # the target app's permission-denial banner, the default read it as noise and
    # 1600 reads it exactly. Costs ~20% per frame on a dense page (1.9s -> 2.3s)
    # and is the difference between detecting a declared runtime condition and
    # reporting a checkpoint that did not hold.
    ocr_det_side_len: int = 1600

    # --- Determinism ----------------------------------------------------------
    # Two consecutive hash-equal frames means the page settled. Kills the
    # async-reflow race without a single sleep().
    settle_poll_ms: int = 120
    settle_timeout_ms: int = 8000
    step_timeout_ms: int = 15000

    # --- Secrets --------------------------------------------------------------
    # Substituted in the action layer, below the point where anything is
    # serialized. Never travels through an artifact, a log line, or a prompt.
    target_username: str = ""
    target_password: str = ""

    @property
    def viewport(self) -> Viewport:
        return Viewport(width=self.display_width, height=self.display_height)

    def policy_path(self, app: str | None = None) -> Path:
        """The guardrail file for one application.

        Raises rather than falling back to a default policy: running an app under
        another app's allowlist is the one configuration mistake here that could
        let an agent act somewhere it was never permitted.
        """
        name = app or self.default_app
        path = self.policies_dir / f"{name}.yaml"
        if not path.exists():
            known = sorted(p.stem for p in self.policies_dir.glob("*.yaml"))
            raise FileNotFoundError(
                f"no policy for app {name!r} at {path}. Known apps: {known or 'none'}"
            )
        return path

    def apps(self) -> list[str]:
        return sorted(p.stem for p in self.policies_dir.glob("*.yaml"))


@lru_cache
def settings() -> Settings:
    return Settings()
