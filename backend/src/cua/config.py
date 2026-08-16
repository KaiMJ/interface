"""Runtime configuration. One place, environment-driven, no scattered getenv."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .schema import Viewport


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
    target_base_url: str = "http://targetapp:8080"

    # --- Storage --------------------------------------------------------------
    artifacts_dir: Path = Path("/data/artifacts")
    evidence_dir: Path = Path("/data/evidence")
    models_dir: Path = Path("/models")
    policy_file: Path = Path("/app/policies/targetapp.yaml")

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
    # Below this, an OCR line is treated as unreadable rather than as text. Anchor
    # resolution and checkpoints both compare against OCR output, so a confidently
    # wrong string is worse than a missing one.
    ocr_conf_threshold: float = 0.50

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


@lru_cache
def settings() -> Settings:
    return Settings()
