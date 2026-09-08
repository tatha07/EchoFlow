# syntax=docker/dockerfile:1

###############################################################################
# EchoFlow — multi-stage build
#
#   base            : shared OS layer (apt installed ONCE) + non-root runtime user
#   wheelhouse-base : holds the offline ./wheelhouse/ + reqs in a stable layer
#                     that both py-deps stages reference via COPY --from.
#                     Wheelhouse bytes therefore enter the build graph ONCE.
#   py-deps-api     : populates /opt/venv from requirements-base + wheelhouse
#   py-deps-media   : populates /opt/venv + bakes HuggingFace models (secret-fed)
#   api             : gunicorn web + default/fast_feed/celery_beat workers (small)
#   media           : heavy_media worker (FFmpeg libs + baked HF models)
#
# Build:
#   docker compose build
#   docker build --target api -t echoflow-api .
#   docker build --target media -t echoflow-media . \
#       --secret id=hf_token,env=HF_TOKEN     # NEVER pass tokens via --build-arg
#
# Design notes:
#   * Python dependencies live in an isolated /opt/venv inside builder stages.
#     Final images receive ONLY that venv — no /usr/local coupling between
#     builder and runtime OS state, no wheels, no compilers shipped.
#   * HF_TOKEN reaches the bake step exclusively via a BuildKit secret mount.
#     Secrets mounted this way never enter ARG/ENV, layer history, or cache
#     metadata, so `docker history` cannot leak them. Omitting the secret
#     falls back to anonymous downloads of these public models.
#   * Source enters final images through an explicit allowlist — context junk
#     (wheelhouse, docs, frontend, CI configs) can never ride along.
#   * Stage-specific HEALTHCHECKs keep images self-describing for bare
#     `docker run`: api probes HTTP /health/, media pings its own Celery node.
#   * BuildKit named caches (see Dockerfile comments below) survive across
#     builds on the same host:
#       - echoflow-apt  : downloaded .deb files under /var/cache/apt/archives
#       - echoflow-pip  : pip's HTTP/wheel metadata index under /root/.cache/pip
#       - echoflow-hf   : HuggingFace model artifacts under appuser's cache
#     Cache IDs are namespaced to this project so a `docker builder prune`
#     never wipes them by accident; inspect with `docker buildx du`.
#     SECURITY: /var/lib/apt/lists is intentionally NOT cached — caching
#     trusted package indexes can mask security updates. `apt-get update`
#     runs every build so list freshness always wins over re-download speed.
###############################################################################

FROM python:3.11-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd -g 1000 appgroup \
 && useradd -u 1000 -g appgroup -s /bin/bash -m appuser \
 && printf 'Acquire::Retries "10";\nAcquire::http::Timeout "120";\nAcquire::https::Timeout "120";\nAcquire::http::Pipeline-Depth "0";\n' \
      > /etc/apt/apt.conf.d/99custom-network

# BuildKit named cache for downloaded .deb files. Survives across builds on
# this host. uid/gid 0 (root) owns /var/cache/apt in Debian; sharing the cache
# between the root-owned install step and any later user-owned step is fine
# because this stage is only used as a parent image.
#
# SECURITY: not caching /var/lib/apt/lists deliberately — see header note.
# A stale package index can silently serve vulnerable .deb files. Re-running
# `apt-get update` on every build is cheap (one HTTP round-trip per mirror)
# and is the only guarantee that security updates flow into the image.
RUN --mount=type=cache,id=echoflow-apt,target=/var/cache/apt/archives,sharing=locked \
    apt-get update \
 && apt-get install -y --no-install-recommends --fix-missing \
        libpq-dev \
        gcc \
        postgresql-client \
        ffmpeg \
        libsndfile1 \
        libmagic1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# -----------------------------------------------------------------------------
# Dependency builders (never shipped).
#
# The venv is created against THIS image's interpreter; because every stage
# derives from the same `base`, shebangs and pyvenv.cfg remain valid after the
# COPY --from into the finals.
#
# --no-index: wheelhouse is verified complete for this pinned set — installs
# are fully offline and deterministic. If you add a dependency, regenerate the
# wheelhouse first (see AGENTS.md) or temporarily drop --no-index.
# -----------------------------------------------------------------------------

# Stable, narrow stage that owns the offline wheels. Both py-deps stages
# reference it via COPY --from=wheelhouse-base, so the wheelhouse bytes
# enter the layer graph exactly ONCE per build regardless of how many
# downstream stages need them. Re-evaluated only when wheelhouse/ or the
# pinned requirements files change.
FROM base AS wheelhouse-base

COPY requirements-base.txt requirements-media.txt requirements-online.txt constraints.txt ./
COPY wheelhouse/ /wheelhouse/


FROM wheelhouse-base AS py-deps-api

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# BuildKit named cache for pip's HTTP/wheel metadata index. Even with
# --no-index, pip still does resolver work (PEP 517 build-deps, constraint
# checks, METADATA reads); caching /root/.cache/pip makes subsequent
# installs in the same cache ID near-instant after the first cold run.
# Cache mount is per-stage, so it does not leak into the final image.
RUN --mount=type=cache,id=echoflow-pip,target=/root/.cache/pip,sharing=locked \
    pip install --no-cache-dir \
      --default-timeout=120 --retries 10 \
      --no-index --find-links=/wheelhouse \
      -c constraints.txt \
      -r requirements-base.txt


FROM wheelhouse-base AS py-deps-media

# Cache locations are set BEFORE baking so models land at a path we can copy
# out verbatim; the final media stage re-declares the identical values.
ENV HF_HOME=/home/appuser/.cache/huggingface \
    TORCH_HOME=/home/appuser/.cache/torch \
    SENTENCE_TRANSFORMERS_HOME=/home/appuser/.cache/huggingface

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Single resolver pass, fully offline: wheelhouse includes the CPU-only torch
# build (torch-2.8.0+cpu-cp311); constraints.txt pins torch so nothing can
# resolve to a CUDA build.
RUN --mount=type=cache,id=echoflow-pip,target=/root/.cache/pip,sharing=locked \
    pip install --no-cache-dir \
      --default-timeout=1000 --retries 10 \
      --no-index --find-links=/wheelhouse \
      -c constraints.txt \
      -r requirements-media.txt

# Install online-only dependencies from PyPI (not in wheelhouse).
# Add new packages here without regenerating the wheelhouse.
RUN --mount=type=cache,id=echoflow-pip,target=/root/.cache/pip,sharing=locked \
    pip install --no-cache-dir \
      --default-timeout=300 --retries 5 \
      -c constraints.txt \
      -r requirements-online.txt

# Bake HuggingFace models so runtime never needs network access. A failed
# download FAILS THE BUILD deliberately — a half-baked media image is worse
# than no image.
#
# Cache notes:
#   * echoflow-hf is namespaced to appuser's cache so it survives across
#     py-deps-media builds on this host (Whisper base + sentence-transformers
#     + KeyBERT artifacts are ~250 MB combined; saving them once is a
#     ~5-minute win per cold media build).
#   * sharing=locked so two concurrent builds never race on a half-written
#     model file.
#
# Secret handling:
#   --mount=type=secret  -> file exists only during THIS RUN, never persisted
#   `set -eu` (NOT -x!)  -> xtrace would echo the exported token into build logs
#   [ -s ... ] guard     -> absent/empty secret = anonymous public download
#
# The HF cache mount is ephemeral for this RUN; copy to a layer path so
# the final media stage can COPY it. The layer path mirrors the cache path.
RUN --mount=type=secret,id=hf_token \
    --mount=type=cache,id=echoflow-hf,target=/home/appuser/.cache/huggingface,sharing=locked,uid=1000,gid=1000 \
    set -eu; \
    if [ -s /run/secrets/hf_token ]; then \
        export HF_TOKEN="$(cat /run/secrets/hf_token)"; \
    fi; \
    python -c "from faster_whisper import WhisperModel; m = WhisperModel('base', device='cpu', compute_type='int8'); del m"; \
    python -c "from sentence_transformers import SentenceTransformer; m = SentenceTransformer('all-MiniLM-L6-v2'); del m"; \
    python -c "from keybert import KeyBERT; m = KeyBERT(); del m"; \
    # DECISION: BuildKit --mount=type=cache is ephemeral — files written to
    # the cache target are saved to the BuildKit cache store for future
    # builds but are NOT part of this layer's filesystem. A subsequent
    # COPY --from=py-deps-media targeting the cache-mount path fails with
    # "not found". We copy the downloaded models to a regular filesystem
    # path so they persist into the layer and are visible to the media
    # stage's COPY. The cache mount (echoflow-hf) still provides cross-build
    # download speedup on subsequent builds; this cp only copies into the
    # layer, adding ~250 MB to this stage's size but nothing extra at runtime.
    cp -a /home/appuser/.cache/huggingface /home/appuser/hf_baked

# -----------------------------------------------------------------------------
# Final images
# -----------------------------------------------------------------------------

FROM base AS api

COPY --from=py-deps-api /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

LABEL org.opencontainers.image.title="echoflow-api" \
      org.opencontainers.image.description="EchoFlow API server and default/feed/beat Celery workers" \
      org.opencontainers.image.source="https://github.com/devansh1012007/EchoFlow"

# Explicit allowlist (NOT `COPY .`): keeps wheels, docs, frontend, CI configs
# and anything else context-resident OUT of the production image.
# --chown avoids a full layer-duplicating `chown -R` pass.
COPY --chown=appuser:appgroup backend/ ./backend/
COPY --chown=appuser:appgroup manage.py wait_for_db.py gunicorn.conf.py ./
# The feed recommendation engine lives in /ai_ml/. Required by
# backend.app.tasks (re-export shim) which does
# `from ai_ml.pipelines.feed_tasks import refill_user_feed` at
# Django app-loading time. Without this COPY the production image
# would crash on first request.
COPY --chown=appuser:appgroup ai_ml/ ./ai_ml/

# Self-describing liveness for the HTTP role (web is this image's primary
# deployment). celery/celery_feed share this image but answer queue traffic,
# so docker-compose.yml overrides their probe with the Celery-ping variant.
#
# SECURITY: We send `X-Forwarded-Proto: https` because, with
# SECURE_SSL_REDIRECT=True in production, the in-container healthcheck
# would otherwise receive a 301 to https://... and the healthcheck
# follows redirects, so the probe would pass even when the app is
# broken. By pretending the request already came from nginx, we
# exercise the same code path as a real client.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; req = urllib.request.Request('http://localhost:8000/health/', headers={'X-Forwarded-Proto': 'https'}); urllib.request.urlopen(req, timeout=4)" || exit 1

USER appuser

EXPOSE 8000


FROM base AS media

COPY --from=py-deps-media /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    HF_HOME=/home/appuser/.cache/huggingface \
    TORCH_HOME=/home/appuser/.cache/torch \
    SENTENCE_TRANSFORMERS_HOME=/home/appuser/.cache/huggingface

LABEL org.opencontainers.image.title="echoflow-media" \
      org.opencontainers.image.description="EchoFlow heavy_media Celery worker (FFmpeg + baked HuggingFace models)" \
      org.opencontainers.image.source="https://github.com/devansh1012007/EchoFlow"

# Baked-in models from the builder stage, re-owned for the runtime user.
# Source is /home/appuser/hf_baked (a regular filesystem path) — the cache
# mount target /home/appuser/.cache/huggingface is ephemeral and not part
# of the layer. See py-deps-media RUN for the cp that materializes the
# cache contents into a layer-visible path.
COPY --from=py-deps-media --chown=appuser:appgroup \
     /home/appuser/hf_baked /home/appuser/.cache/huggingface

# Same explicit allowlist as the api stage.
COPY --chown=appuser:appgroup backend/ ./backend/
COPY --chown=appuser:appgroup manage.py wait_for_db.py gunicorn.conf.py ./
COPY --chown=appuser:appgroup ai_ml/ ./ai_ml/

# Worker-role liveness: passes ONLY if THIS container's Celery consumer
# answers an inspect ping on its own node name (celery@$(hostname)).
HEALTHCHECK --interval=30s --timeout=15s --start-period=30s --retries=3 \
    CMD celery -A backend.EchoFlow inspect ping -d "celery@$(hostname)" --timeout=10 || exit 1

USER appuser
