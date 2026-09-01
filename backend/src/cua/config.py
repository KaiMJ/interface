"""Runtime configuration. One place, environment-driven, no scattered getenv."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .schema import Viewport

# The container mounts everything under /app and /data; outside it those paths do not exist,
# so each default falls back to the same file in the repository.
_REPO = Path(__file__).resolve().parents[3]


def _path(container: str, in_repo: str) -> Path:
    installed = Path(container)
    return installed if installed.exists() else _REPO / in_repo


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUA_", env_file=".env", extra="ignore")

    # --- LLM (discovery only; replay runs with all of this unset) ------------
    # Provider-qualified model string; LiteLLM reads the credential env var directly, and it
    # is deliberately not copied onto this object — a key here reaches a settings dump.
    #
    # Must support vision AND tool calling: screenshots in, one tool call out.
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
    # Where this deployment's install lives; empty means "whatever the policy declares". The
    # tenant seam: the policy describes the vendor product, this URL one institution's install.
    target_base_url: str = ""

    # --- Storage --------------------------------------------------------------
    artifacts_dir: Path = Field(default_factory=lambda: _path("/data/artifacts", "artifacts"))
    evidence_dir: Path = Field(default_factory=lambda: _path("/data/evidence", "evidence"))
    # One file per application, selected by `--app`. Mounted rather than baked into the image,
    # so changing a guardrail does not need a rebuild.
    policies_dir: Path = Field(
        default_factory=lambda: _path("/app/policies", "policies")
    )
    # Only reached by `cua discover`, which runs before any artifact exists to say
    # which app it belongs to.
    default_app: str = "targetapp"

    # --- Perception -----------------------------------------------------------
    # `omniparser` is the real one; `ocr_only` lets replay and the tests run
    # without a 2GB torch install.
    detector: str = "omniparser"
    # Fetched from HuggingFace on first use, so the image never carries ~300MB of weights.
    # The upstream release, not the TorchScript archive, which ultralytics refuses to load.
    omniparser_repo: str = "microsoft/OmniParser-v2.0"
    omniparser_repo_file: str = "icon_detect/model.pt"
    detect_conf_threshold: float = 0.30
    merge_iou_threshold: float = 0.60
    # `onnxruntime` is the CPU default; `torch` runs the same PP-OCR models through the
    # detector's torch install, the only way to reach the GPU — onnxruntime-gpu ships CUDA 12
    # wheels against this image's CUDA 13 and registers no device. OCR is ~95% of a replay's
    # wall clock; `scripts/bench_perception.py` measures both.
    ocr_engine: str = "onnxruntime"
    # Below this a line is unreadable rather than text. Anchors and checkpoints both
    # compare against OCR, so a confidently wrong string is worse than a missing one.
    ocr_conf_threshold: float = 0.50
    # Longest side PP-OCR's detector sees. Its default downscales 1440x900 far enough to lose
    # small coloured text, such as the permission-denial banner. ~20% cost per frame.
    ocr_det_side_len: int = 1600

    # --- Determinism ----------------------------------------------------------
    # Two hash-equal frames means settled. Kills the async-reflow race, no sleep().
    settle_poll_ms: int = 120
    settle_timeout_ms: int = 8000
    step_timeout_ms: int = 15000

    # --- Secrets --------------------------------------------------------------
    # Substituted in the action layer, below serialization. Never reaches an
    # artifact, a log line or a prompt.
    target_username: str = ""
    target_password: str = ""

    @property
    def viewport(self) -> Viewport:
        return Viewport(width=self.display_width, height=self.display_height)

    def policy_path(self, app: str | None = None) -> Path:
        """The guardrail file for one application.

        Raises rather than falling back to a default policy: running an app under another
        app's allowlist would let an agent act somewhere it was never permitted.
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
