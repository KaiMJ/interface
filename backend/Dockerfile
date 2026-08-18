# The automation container: a real (virtual) desktop that both the agent and a
# human operator can drive.
#
# Lineage: a trimmed fork of Anthropic's computer-use-demo image. We keep the
# desktop plumbing (Xvfb + window manager + x11vnc + noVNC) because that is what
# makes the §3.6 handoff real rather than mocked — the human takes over the same
# X display the agent is driving. We drop the demo's agent loop and Streamlit UI
# and substitute our own backend.
#
# Two stages. The builder resolves and installs Python dependencies into a venv;
# the runtime copies that venv and adds only what has to exist at run time. That
# leaves uv, the resolver's caches, and the build toolchain behind.
#
# It also drops Playwright's official image, which ships Chromium, Firefox AND
# WebKit plus their combined dependency closure. We drive one browser. Installing
# `chromium` alone from a plain slim base costs about a gigabyte less.

ARG PYTHON_VERSION=3.12-slim-bookworm

# ---------------------------------------------------------------------------
# builder — dependencies only
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependency layer, resolved from the lockfile and nothing else. `--frozen` means
# the build fails if uv.lock is stale rather than silently resolving something
# different from what was tested — which is the entire point of committing a lock.
# `--no-install-project` keeps this layer independent of application code, so an
# edit to src/ does not trigger a multi-gigabyte reinstall.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra perception

# Project layer.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra perception

# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISPLAY=:1 \
    PATH="/app/.venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Desktop: a virtual display, a window manager (so windows take focus and stack
# the way they do on a real machine — without one, keyboard input silently goes
# nowhere), a VNC server, and the browser-side VNC client.
#
# `--no-install-recommends` matters here: mutter's recommends alone pull in a
# few hundred megabytes of desktop session cruft we never start.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        x11vnc \
        xdotool \
        scrot \
        x11-utils \
        mutter \
        dbus-x11 \
        curl \
        ca-certificates \
        procps \
        # OpenMP runtime, needed by opencv and onnxruntime. The slim base omits
        # it, and torch bundles a private copy — so anything importing torch first
        # appears to work while a standalone import fails. An accidental
        # dependency on import order is worse than a missing package.
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# noVNC + websockify, pinned. Serves a VNC client over HTTP so the console can
# embed the live session with no native client install. Independently versioned
# from the console's `@novnc/novnc` dependency: this side is only the proxy.
ARG NOVNC_VERSION=1.5.0
ARG WEBSOCKIFY_VERSION=0.12.0
RUN curl -fsSL "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz" \
      | tar -xz -C /opt && mv "/opt/noVNC-${NOVNC_VERSION}" /opt/novnc \
 && curl -fsSL "https://github.com/novnc/websockify/archive/refs/tags/v${WEBSOCKIFY_VERSION}.tar.gz" \
      | tar -xz -C /opt && mv "/opt/websockify-${WEBSOCKIFY_VERSION}" /opt/novnc/utils/websockify \
 && ln -s /opt/novnc/vnc.html /opt/novnc/index.html \
 && rm -rf /opt/novnc/tests /opt/novnc/docs /opt/novnc/.git*

WORKDIR /app

# The resolved environment, built once, carried over whole.
COPY --from=builder /app/.venv /app/.venv

# Chromium only, plus its system libraries. Must run after the venv is on PATH —
# `playwright` is a dependency, not a system package.
RUN playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

# RapidOCR caches model weights inside its own package directory, and the
# container runs as the invoking user. The ONNX weights ship in the wheel, but the
# torch ones — the only route to this machine's GPU, and OCR is ~95% of a replay's
# wall clock — are fetched on first use, which a read-only directory turns into a
# failure halfway through the first run. Writable here so that `CUA_OCR_ENGINE=torch`
# can provision itself; see `cua.perception.ocr`.
RUN chmod -R a+w /app/.venv/lib/python3.12/site-packages/rapidocr/models

COPY src ./src
COPY fixtures ./fixtures
COPY image ./image
COPY scripts ./scripts
RUN chmod +x ./image/*.sh

# Model weights are NOT baked in. Compose bind-mounts ./models here — ~300MB
# that a rebuild must not re-download and that git must never see.
# Compose runs this container as the invoking uid so that artifacts/ and
# evidence/ come out owned by the developer rather than by root. That uid has no
# /etc/passwd entry, so HOME falls back to "/" — which is not writable, and which
# makes matplotlib, fontconfig and every other library that wants a dot-directory
# fail noisily on import. Give the container a home that any uid can write to.
ENV HOME=/tmp/home \
    XDG_CACHE_HOME=/tmp/home/.cache \
    XDG_CONFIG_HOME=/tmp/home/.config \
    MPLCONFIGDIR=/tmp/home/.cache/matplotlib

# Weights live in the bind-mounted volume, not the image: ~270MB that a rebuild
# must not re-download and that git must never see. Only the small ONNX models
# that ship inside the rapidocr wheel are in the image (32MB, version-pinned to
# the package, nothing to download at runtime).
ENV CUA_MODELS_DIR=/models \
    HF_HOME=/models/hf \
    YOLO_CONFIG_DIR=/models/ultralytics \
    PYTHONPATH=/app/src

# 0777 rather than a fixed owner: the runtime uid is not known at build time.
RUN mkdir -p /models /data/artifacts /data/evidence \
             /tmp/home/.cache/matplotlib /tmp/home/.config \
 && chmod -R 0777 /tmp/home /models /data

EXPOSE 8000 5900 6080

ENTRYPOINT ["./image/entrypoint.sh"]
