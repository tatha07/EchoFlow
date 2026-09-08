# EchoFlow — Agent Quick-Start

## Stack
Django 5.2 / DRF 3.18 · PostgreSQL 16 + pgvector (HNSW) · Redis 7 · Celery + Celery Beat · FFmpeg (HLS) · Vite/React (frontend/) · nginx 1.27 (TLS terminator) · Prometheus + Grafana (observability) · Sentry (errors, ready-to-configure)

> **Docker is the only supported way to run EchoFlow locally.** There is no bare-metal install path. The `Dockerfile` and `docker-compose.yml` provision every dependency (Postgres+pgvector, Redis, MinIO, all Celery queues, ffmpeg, Python 3.11, ML libs, nginx, Prometheus, Grafana) in a single `docker compose up --build`. For production at small scale (~$6/month), use the hybrid deployment: `docker-compose.vps.yml` on a VPS + `docker-compose.laptop.yml` on a laptop + Cloudflare R2 for object storage. See [docs/EXPLAIN/DEPLOYMENT/01-hybrid-deployment-overview.md](docs/EXPLAIN/DEPLOYMENT/01-hybrid-deployment-overview.md).

## Docker
```bash
docker compose up --build          # 14 services: db, pgbouncer, redis_broker, redis_cache, minio, minio-init, nginx, web, celery, celery_feed, celery_media, celery_beat, prometheus, grafana
docker compose down                # tear down
docker compose logs -f celery_media
docker compose exec web python manage.py migrate

# Observability endpoints (after `docker compose up`):
#   Prometheus: http://localhost:9090
#   Grafana:    http://localhost:3000  (admin / ${GRAFANA_ADMIN_PASSWORD})
```

> **nginx is the only public-facing entrypoint.** It terminates TLS on `:80` (redirect) / `:443` (Django) / `:9443` (MinIO HLS) and forwards plain HTTP to the in-network backends. The `web:8000` and `minio:9000` ports are NOT directly reachable from the host anymore (except `web:8005` which is published as a debug escape hatch). To verify the stack is up: `curl -kI https://localhost/health/`. See [docs/EXPLAIN/docker/05-https-tls-termination.md](docs/EXPLAIN/docker/05-https-tls-termination.md) for the full design.

## Running Tests

> **All tests run inside the Docker `web` container against PostgreSQL.**
> The test database is `echoflow_test` — auto-created by conftest on first
> run. No SQLite, no stub migrations, no bare-metal test mode.

### Quick start (full stack + tests)

```bash
# Build and start test stack (db, redis, minio, web)
docker compose -f docker-compose.yml -f docker-compose.test.yml up --build -d

# Run the full test suite (conftest auto-creates echoflow_test DB)
docker compose exec -e PYTHONPATH=/app web pytest backend/app/tests/ --tb=short

# Run a single test file
docker compose exec -e PYTHONPATH=/app web pytest backend/app/tests/test_adversarial_pass3.py -v

# Run a single test class
docker compose exec -e PYTHONPATH=/app web pytest backend/app/tests/test_adversarial_pass3.py::TestN1CommentAuthorization -v

# Run the migration / config / static checks that CI runs
docker compose exec web python manage.py migrate --noinput
docker compose exec web python manage.py makemigrations --check --dry-run
docker compose exec web python manage.py check --fail-level WARNING
docker compose exec web python manage.py collectstatic --noinput --dry-run

# Inspect coverage (with the pytest-cov plugin — installed in the image)
docker compose exec -e PYTHONPATH=/app web pytest backend/app/tests/ --cov=backend.app --cov-report=term-missing

# Tear down test stack
docker compose -f docker-compose.yml -f docker-compose.test.yml down -v
```

### Hybrid Deployment (production at small scale)

The hybrid deployment splits services across a VPS (light services) and a
laptop (heavy media worker), with Cloudflare R2 for object storage:

```bash
# VPS: light services (8 containers)
git checkout feat/hybrid-vps
cp .env.vps.example .env
# Edit .env with real values
bash scripts/vps-deploy.sh

# Laptop: heavy media worker (1 container)
git checkout feat/hybrid-laptop
cp .env.laptop.example .env
# Edit .env with real values (DJANGO_SECRET_KEY must match VPS)
bash scripts/laptop-deploy.sh
```

- `docker-compose.vps.yml` — slimmed 8-service compose (removes pgbouncer,
  minio, celery_media, prometheus, grafana)
- `docker-compose.laptop.yml` — single-service compose (celery_media only)
- `scripts/vps-deploy.sh` — one-shot VPS deploy (build, migrate, collectstatic,
  Tailscale setup, daily backup cron)
- `scripts/laptop-deploy.sh` — one-shot laptop deploy (build media image,
  start worker, start heartbeat)
- `scripts/laptop-heartbeat.sh` — background heartbeat writer to Redis
- `backend/app/views/system_health.py` — `/api/v1/health/media-worker/`
  endpoint reporting laptop worker liveness

See [docs/EXPLAIN/DEPLOYMENT/](docs/EXPLAIN/DEPLOYMENT/) for full setup guides.

### Production stack + tests (when you need nginx/MinIO endpoints)

```bash
# Start full stack (db, pgbouncer, redis, minio, nginx, web, celery, etc.)
docker compose up --build -d

# Run tests against the full stack
docker compose exec -e PYTHONPATH=/app web pytest backend/app/tests/ --tb=short

# Tear down
docker compose down
```

## Observability

Two stacks are available after `docker compose up`:

### Prometheus + Grafana (primary)
```bash
# Prometheus: scrape target, query PromQL
open http://localhost:9090/targets      # confirm web target is UP
open http://localhost:9090/graph        # PromQL query editor

# Grafana: dashboards, datasources auto-provisioned
open http://localhost:3000              # admin / ${GRAFANA_ADMIN_PASSWORD}
#   Dashboards > EchoFlow > 01-feed-and-suggestions
#   Dashboards > EchoFlow > 02-celery-health
```

The scraper reads `/metrics/` from `web` every 15s. Two dashboards ship pre-built:
- **01-feed-and-suggestions.json** — p95 of `feed_refill_duration_seconds`, `suggestion_ranking_duration_seconds`, cache hit/miss rate.
- **02-celery-health.json** — `rate(celery_tasks_processed_total[5m])` by queue/task, p95 of `hls_processing_duration_seconds`.

Alert rules are intentionally not shipped in this pass — the audit doc proposes them; this is a follow-up. Add them in `docker/prometheus/alerts.yml` and reload Prometheus when ready.

Full design: [docs/EXPLAIN/observability/03-prometheus-grafana-design.md](docs/EXPLAIN/observability/03-prometheus-grafana-design.md). Activation runbook: [docs/EXPLAIN/observability/04-prometheus-grafana-setup.md](docs/EXPLAIN/observability/04-prometheus-grafana-setup.md).

### Sentry (error capture, ready-to-configure)
`sentry-sdk[django,celery]==2.18.0` is installed; `init_sentry()` runs in each process's `App1Config.ready()`. Errors from web + all 4 celery services are captured when `SENTRY_DSN` is set in `.env`. Gated on `DJANGO_DEBUG=False` so dev/test paths are no-ops.

```bash
# Local: capture_exception is a no-op (no DSN, debug=True)
# Staging/prod: set SENTRY_DSN, SENTRY_ENV in .env
echo 'SENTRY_DSN=https://abc123@sentry.io/456' >> .env
echo 'SENTRY_ENV=production' >> .env
docker compose up -d --force-recreate web celery celery_feed celery_media celery_beat
```

The `capture_exception(exc, **context)` wrapper in `backend/app/services/sentry.py` attaches the current request's `correlation_id` (from `backend.EchoFlow.correlation`) as a Sentry tag, so production errors cross-reference with the worker's correlation_id (Group B item 11). `send_default_pii=False` — user IPs, cookies, and auth headers are NOT sent.

### Observability TUI (dev fallback)
```bash
# Run inside the web container (TUI requires urllib; stdlib only, no extra deps)
docker compose exec web python scripts/observability_tui.py

# One-shot snapshot to stdout (useful for scripting)
docker compose exec web python scripts/observability_tui.py --once

# Point at a different /metrics/ URL (e.g. against a staging server)
docker compose exec web python scripts/observability_tui.py --url http://staging:8005/metrics/

# Refresh every 2 seconds instead of 5
docker compose exec web python scripts/observability_tui.py --interval 2
```

The TUI reads `/metrics/` from the running `web` container and prints a text dashboard of the 6 custom application metrics (`echoflow_feed_refill_duration_seconds`, `echoflow_suggestion_ranking_duration_seconds`, `echoflow_toggle_like_duration_seconds`, `echoflow_cache_get_set_duration_seconds`, `echoflow_hls_processing_duration_seconds`, `echoflow_celery_tasks_processed_total`). Refreshes every N seconds (default 5). It is now a dev fallback — Grafana is the primary observability tool.

**Why `docker compose exec web` and not `docker compose run web`?**
- `exec` runs the command in the already-running `web` service (uses its env, mounted volumes, and depends_on the DB/Redis/MinIO). This matches the actual production-like runtime.
- `run` would spin up a fresh container that doesn't have the dependent services linked unless you pass `--service-ports` and explicitly `depends_on` them — which complicates the command for no benefit.

**When a test genuinely needs bare-metal (rare, e.g. debugging an ML model locally):** run the wheelhouse install documented in commit history of the audit-pass-3 dump, set the same env vars the container uses (DATABASE_URL, REDIS_BROKER_URL, REDIS_CACHE_URL, etc.), and run pytest directly. Document the divergence in the PR description.

### Dockerfile architecture
Single multi-stage `Dockerfile` with five stages (two are build-only):

| Stage | Shipped? | Purpose |
|---|---|---|
| `base` | parent of all | apt union (libpq-dev, gcc, postgresql-client, ffmpeg, libsndfile1), appuser (UID 1000) |
| `py-deps-api` | yes, this is preferred | installs requirements-base.txt offline from wheelhouse into site-packages |
| `py-deps-media` | yes, this is preferred | requirements-media.txt + bakes HuggingFace models into the `echoflow-hf` cache mount, then `cp -a` to `/home/appuser/hf_baked` so the models persist into the layer (see "HuggingFace bake copy-to-layer" below) |
| `api` | yes | web, celery, celery_feed, celery_beat — small image, no wheels/models |
| `media` | yes | celery_media — `COPY --from=py-deps-media /home/appuser/hf_baked /home/appuser/.cache/huggingface`; runtime `HF_HOME=/home/appuser/.cache/huggingface` |

Final images receive dependencies via `COPY --from=py-deps-* /opt/venv /opt/venv`
and source via an explicit allowlist (`backend/` — incl. `wait_for_db.py`
and `gunicorn.conf.py`, `manage.py`) — never a blanket `COPY .`. Stage-specific HEALTHCHECKs are
baked in: `api` probes `GET /health/` (compose overrides it to a Celery ping
for the worker services sharing that image); `media` pings its own Celery node.
HF_TOKEN is delivered ONLY via BuildKit secret mount
(`--mount=type=secret,id=hf_token`) — never `--build-arg`, which would persist
the token in builder layer history readable by `docker history`.

**HuggingFace bake copy-to-layer** (added 2026-09-07, fixed the `celery_media` build):

BuildKit `--mount=type=cache` is **ephemeral** — the cache target is a temporary
overlay that exists only during the `RUN` command. Files written into the
cache mount are saved to the BuildKit cache store (for future build speedup)
but are **not** part of the committed layer's filesystem. A subsequent
`COPY --from=py-deps-media <cache-mount-path>` therefore fails with
`not found` — the path exists during the `RUN` but is invisible to the
layer graph.

The fix: after the model download commands, the `py-deps-media` RUN ends
with `cp -a /home/appuser/.cache/huggingface /home/appuser/hf_baked`. This
materializes the cache contents into a regular filesystem path that **does**
persist into the layer. The `media` stage then `COPY --from=py-deps-media
/home/appuser/hf_baked /home/appuser/.cache/huggingface` lands the baked
models at the runtime `HF_HOME` path unchanged. Runtime env vars
(`HF_HOME`, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` in
`docker-compose.yml`) are NOT modified.

Tradeoff: ~250 MB is now in both the BuildKit `echoflow-hf` cache AND the
layer. The BuildKit cache is for cross-build speed (it only costs disk
locally and is not in the image); the layer copy is what's shipped. Final
image size is unchanged at the user-visible layer.

CI guards against regression: `.github/workflows/docker-image.yml` runs a
smoke test on every PR that builds the `media` target, loading the
freshly-built image and asserting the baked HF models load in
`HF_HUB_OFFLINE=1` mode.

```bash
# Build all targets
docker compose build

# Build a single target manually (media consumes HF_TOKEN as a build SECRET)
docker build --target api   -t echoflow-api  .
export HF_TOKEN=hf_xxx   # or: --secret id=hf_token,src=./hf_token.txt
docker build --target media -t echoflow-media . --secret id=hf_token,env=HF_TOKEN

# Override image tag
docker compose build --build-arg TAG=dev
```

### Offline wheelhouse
All pip installs use `--no-index --find-links=/wheelhouse`. The wheelhouse is a local directory of pre-built wheels that makes builds fully offline and deterministic.

**Regenerate the wheelhouse** (run inside a py3.11 container):
```bash
mkdir -p wheelhouse-new
docker run --rm \
  -v "$PWD/requirements-base.txt:/req/requirements-base.txt:ro" \
  -v "$PWD/requirements-media.txt:/req/requirements-media.txt:ro" \
  -v "$PWD/constraints.txt:/req/constraints.txt:ro" \
  -v "$PWD/wheelhouse-new:/out" \
  python:3.11-slim-bookworm sh -c "\
    pip wheel --no-deps -w /out 'dj-rest-auth==7.2.0' && \
    pip download --prefer-binary --retries 10 --timeout 120 \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      -c /req/constraints.txt -r /req/requirements-base.txt -d /out && \
    pip download --prefer-binary --retries 10 --timeout 120 \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      -c /req/constraints.txt -r /req/requirements-media.txt -d /out"

rm -rf wheelhouse && mv wheelhouse-new wheelhouse
```

**Important rules:**
- After regenerating, `rm -rf wheelhouse && mv wheelhouse-new wheelhouse` swaps the directory.
- If you add/change any pin in `requirements-base.txt`, `requirements-media.txt`, or `constraints.txt`, **re-run the regen script first**.
- The `pip wheel --no-deps dj-rest-auth` step pre-builds a wheel for dj-rest-auth (it is sdist-only on PyPI).
- `librosa==0.11.0` — do NOT bump to 1.x (requires Python >= 3.12).
- `django==5.2.17` — do NOT bump to 6.x (requires Python >= 3.12).
- `sentry-sdk[django,celery]==2.18.0` — added when Sentry integration landed (Group B partial-issues, PR 2 of 3). The wheelhouse regen script must include this.

### Pop!_OS note
Uses Docker Compose V2 (`docker compose`, not `docker-compose`). If you have `docker-compose` installed from an old PPA, it conflicts with the V2 plugin — remove it with `sudo apt remove docker-compose` and use `docker compose` instead.

### BuildKit cache (named, persistent across builds)

The `Dockerfile` declares three **named** BuildKit cache mounts so cold builds skip the expensive network round-trips on subsequent runs:

| Cache ID | Mounted at | What it holds | Saved per build |
|---|---|---|---|
| `echoflow-apt` | `/var/cache/apt/archives` | Downloaded `.deb` files (`libpq-dev`, `gcc`, `ffmpeg`, `libsndfile1`, `libmagic1`, `postgresql-client`) | ~200 MB; ~1-2 min |
| `echoflow-pip`  | `/root/.cache/pip`     | pip's HTTP/wheel metadata index (resolver cache) | Seconds — only helps repeated installs in the same build |
| `echoflow-hf`   | `/home/appuser/.cache/huggingface` | Baked HF model artifacts (Whisper `base`, `all-MiniLM-L6-v2`, KeyBERT) | ~250 MB; ~5 min on `media` rebuild |

A dedicated `wheelhouse-base` stage owns the offline `./wheelhouse/` so both `py-deps-api` and `py-deps-media` reference it via `COPY --from=wheelhouse-base` — wheelhouse bytes enter the layer graph exactly once per build.

**Inspect / manage the caches:**

```bash
docker buildx du                                    # show every named cache and its size
docker buildx du --filter type=buildkit             # only BuildKit-managed caches
docker buildx prune --filter type=buildkit          # safe — never touches named caches by default
docker buildx prune --filter id=echoflow-apt        # nuke one specific cache (e.g. after adding a new apt package)
docker builder prune                                # CAREFUL — wipes dangling builders; named caches survive by default
```

**Cache invalidation rules:**
- Adding/changing a package in `Dockerfile` apt-get list invalidates the `base` stage → next build re-downloads everything → caches are repopulated transparently.
- The `wheelhouse/` directory changing (new wheels added) invalidates `wheelhouse-base` → both py-deps stages rebuild.
- HuggingFace model upgrade → invalidate manually with `docker buildx prune --filter id=echoflow-hf`. There is no automatic signal from inside the build that the upstream model changed.
- CI runners (GitHub Actions) start with empty BuildKit **named** caches (echoflow-apt / echoflow-pip / echoflow-hf) — they only speed up repeated local builds on the same machine. The **layer** cache IS persisted across CI runs via `cache-from: type=gha,scope=${{ matrix.target }}` in `.github/workflows/docker-image.yml`. The scope is per-matrix-target so a source-only change doesn't bust the heavy media layer cache and vice-versa. The named mount caches (especially `echoflow-hf` at ~250 MB) re-download on every CI run; if that becomes a CI cost issue, see `docs/EXPLAIN/docker/01-multi-stage-dockerfile.md` §"BuildKit cache" for the registry-backed upgrade.

**What is intentionally NOT cached:**
- `/var/lib/apt/lists/` — stale package indexes can silently serve vulnerable `.deb` files. `apt-get update` runs on every build; security wins over re-download speed.

### Runtime notes
- Web container runs: `backend/wait_for_db.py → migrate → collectstatic → gunicorn -c backend/gunicorn.conf.py`.
- `gunicorn.conf.py` uses `preload_app=True` with a `post_fork` hook that resets Django DB connections (critical because `EchoFlow/__init__.py` imports Celery, which creates Redis connections in the master process before fork).
- Health checks: `GET /health/` (liveness), `GET /ready/` (readiness — checks DB), `GET /metrics/` (Prometheus).
- Resource limits defined per-service in `docker-compose.yml` under `deploy.resources`.
- Override gunicorn workers/threads with `GUNICORN_WORKERS` and `GUNICORN_THREADS` env vars.
- **Do NOT** mount `huggingface_cache` volume on `celery_media` — models are baked into the image at build time.
- **PgBouncer (Phase 1.0):** web/celery services connect via `pgbouncer:6432` (transaction pool mode, `AUTH_TYPE=scram-sha-256`). Non-Docker dev (`pip install + runserver`) skips pgbouncer and connects to `localhost:5432` directly via `DATABASE_URL` — both paths work.
- **Split Redis (Phase 1.0):** Docker runs `redis_broker` (noeviction, 512MB, `REDIS_BROKER_URL`) and `redis_cache` (LRU, 1GB, `REDIS_CACHE_URL`) as separate services. Non-Docker dev sets `REDIS_URL` only — both URLs fall back to it.

## Environment Variables (required)
| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY` | Django signing key — app fails without it |
| `DJANGO_DEBUG` | **Must be `False` in any environment behind the nginx terminator.** Enables the `if not DEBUG:` block (`SECURE_SSL_REDIRECT`, HSTS, secure cookies). Default: `False` in `.env.example`. |
| `DATABASE_URL` | Docker: `postgres://user:pass@pgbouncer:6432/echoflow_db`. Non-Docker dev: `postgres://user:pass@localhost:5432/echoflow_db` |
| `READ_DATABASE_URL` | Optional. When set, activates the read-replica routing in `backend/app/db_routers.py`. Postgres URL of the streaming replica. See [docs/EXPLAIN/database/05-read-replica-design.md](docs/EXPLAIN/database/05-read-replica-design.md) for the activation playbook. |
| `REDIS_URL` | Non-Docker dev: `redis://localhost:6379/1` (single Redis). Optional in Docker. |
| `REDIS_BROKER_URL` | Docker: `redis://redis_broker:6379/0`. Falls back to `REDIS_URL`. |
| `REDIS_CACHE_URL` | Docker: `redis://redis_cache:6379/0`. Falls back to `REDIS_URL`. |
| `HF_TOKEN` | HuggingFace token (model baking at build time). See [docs/EXPLAIN/operations/hf-token-rotation.md](docs/EXPLAIN/operations/hf-token-rotation.md) for the rotation runbook. |
| `OPENAI_API_KEY` | Optional — reserved for OpenAI pipeline branch |
| `FREESOUND_API_KEY` | Required only for freesound scraper |
| `SEED_AUTH_TOKEN` | Auth token for `seed_db.py` |
| `GUNICORN_WORKERS` | Default gunicorn workers (default: 4) |
| `GUNICORN_THREADS` | Default gunicorn threads (default: 4) |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated allowed hosts. Must include every host the nginx terminator is reached at (`localhost`, your prod hostname, any Tailscale/CNAMES). Default: `localhost`. |
| `DJANGO_CORS_ALLOWED_ORIGINS` | Comma-separated **https://** origins. Every browser-reachable origin MUST be `https://` once the terminator is live — `http://` here causes mixed-content / CORS preflight failures. |
| `PUBLIC_MEDIA_ENDPOINT_URL` | Browser-facing MinIO origin for HLS playback. **Must be `https://`** (e.g. `https://localhost:9443` in dev). `AWS_S3_ENDPOINT_URL` (containers' in-network URL) stays `http://minio:9000`. |
| `MEDIA_TOKEN_SECRET` | HMAC signing key for HLS playback tokens. Shared between Django (issuance) and the Cloudflare Worker or nginx njs (validation). Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Must match the Worker secret set via `npx wrangler secret put MEDIA_TOKEN_SECRET`. See `docs/EXPLAIN/storage/04-hls-token-protection.md`. |
| `MEDIA_TOKEN_TTL_SECONDS` | HLS token time-to-live in seconds. Default `600` (10 min). |
| `MEDIA_TOKEN_COOKIE_DOMAIN` | Cookie `Domain` attribute for the HLS token cookie. Set to parent domain (e.g. `.echo-flow.in`) for cross-subdomain cookies in production. Leave empty for dev (`localhost`). |
| `SENTRY_DSN` | Optional. When set, the `sentry-sdk` in each process captures uncaught exceptions. Get a DSN from sentry.io (free tier works). |
| `SENTRY_ENV` | Sentry environment tag (e.g. `production`, `staging`). Default: `production`. |
| `SENTRY_TRACES_SAMPLE_RATE` | Fraction of requests traced (0.0-1.0). Default: `0.1`. Lower for high-traffic. |
| `SENTRY_PROFILES_SAMPLE_RATE` | Fraction of profiled requests. Default: `0.05`. |
| `GRAFANA_ADMIN_PASSWORD` | Initial admin password for Grafana (first-boot only). Required — Grafana v11 refuses to start without one. |
| `TERMS_VERSIONS` | Comma-separated consent versions (e.g. `v1.0,v1.1`). Used by `RegisterSerializer` and `ConsentAudit` (`terms_version_id`). Default: `v1.0`. See `settings.py:622`. |
| `COMPLIANCE_OFFICER_NAME` | Chief Compliance Officer name (IT Rules 2021 Rule 4(1)(b)). Served by `/legal/compliance/`. Default: `EchoFlow Compliance Officer`. |
| `COMPLIANCE_OFFICER_EMAIL` | CCO email. Default: `compliance@echoflow.in`. |
| `GRIEVANCE_OFFICER_NAME` | Grievance Officer name (IT Rules 2021 Rule 4(1)(a)). Default: `EchoFlow Grievance Officer`. |
| `GRIEVANCE_OFFICER_EMAIL` | Grievance email. Default: `grievance@echoflow.in`. |
| `NODAL_CONTACT_NAME` | Nodal Contact name (IT Rules 2021 Rule 4(1)(c)). Default: `EchoFlow Nodal Contact`. |
| `NODAL_CONTACT_EMAIL` | Nodal email. Default: `nodal@echoflow.in`. |
| `AWS_S3_REGION_NAME` | **Must be `ap-south-1`** (or `ap-south-2`) for DPDP cross-border + RBI data-localisation compliance. `STORAGES` uses this (`settings.py:467`). Default in `.env.example`: `auto` — production must override. |
| `PHYSICAL_ADDRESS` | Registered office / physical address (IT Rules 2021 / Consumer Protection). Not yet exposed in `/legal/compliance/` endpoint (open). |

## Indian Regulatory Compliance — Backend Changes

This section documents all backend changes made to comply with:
- **DPDP Act 2023** (Digital Personal Data Protection Act) — consent, children's data, DPO, breach notification, cross-border
- **IT Rules 2021** (Intermediary Guidelines) — grievance officer, nodal contact, compliance officer, traceability, content moderation
- **CERT-In Directions 2022** — 180-day log retention, 6-hour breach notification
- **Copyright Act 1957** — user upload licensing, attribution
- **Consumer Protection (E-Commerce) Rules 2020** — grievance redressal, country of origin
- **RBI Data Localisation** — financial data must reside in India

### Phase A — DPDP Consent & Age Gating (COMPLETED)

**Models (`backend/app/models.py`):**
- Added `User.is_minor` (BooleanField, default=False) — computed from DOB at registration
- Added `User.minor_consent_verified` (BooleanField, default=False) — parent consent for minors
- Added `User.consent_accepted` (BooleanField, default=False) — explicit consent flag
- Added `User.dob` (DateField, nullable) — date of birth for age gate
- Added `User.parent_email` (EmailField, nullable) — for minor consent flow
- Added `ConsentAudit` model (lines 61-77) — immutable audit trail: `user`, `consent_issued_at`, `terms_version_id`, `privacy_version_id`, `ip_address`, `user_agent`, `withdrawn_at`, `identity_retained_until` (CERT-In 180-day retention)
- Added `CheckConstraint` on `AudioClip.likes`, `shares`, `skips`, `comment_count` >= 0 (DB-level negative counter prevention)

**Serializers (`backend/app/serializers.py`):**
- `RegisterSerializer` now requires `consent_accepted` (BooleanField, required=True) and `terms_version` (validated against `TERMS_VERSIONS` env var)
- Added `dob` and `parent_email` fields for age gate
- Validation logic computes `is_minor` from DOB; if minor, `minor_consent_verified` defaults False (requires parent flow)
- Creates `ConsentAudit` row on successful registration (audit trail persists even if user creation rolls back)
- Magic-byte audio validation (lines 16-21, 128-133) — pure-Python allowlist + python-magic layer-2 check before ffmpeg
- Copyright acknowledgment enforcement (lines 178-191) — user must acknowledge before DB persistence
- Duration probe at upload (lines 236-251) — prevents 24h WAV abuse via pydub/ffprobe
- Comment text sanitization (lines 350-365) — null-byte / control-char stripping
- `watch_time_ms` capped at 10h (lines 373-376) — prevents viewbot inflation

**Views (`backend/app/views/auth.py`):**
- Registration endpoint accepts consent fields, creates `ConsentAudit` via serializer
- `/auth/register/` returns access + refresh tokens with consent confirmation

**Tests (`backend/app/tests/test_auth_regulatory.py`, `test_security_and_validation.py`):**
- `test_register_success` validates consent fields required
- `test_user_has_dob_and_computed_is_minor` uses `date()` objects for DOB
- Compliance endpoint requires auth + returns JSON

### Phase B — Grievance & Compliance Officers (COMPLETED)

**Models (`backend/app/models.py`):**
- Added `Grievance` model (lines 269-295) — DB table per audit: `user`, `category`, `description`, `status`, `assigned_officer`, `resolution`, `created_at`, `resolved_at`, `escalated`, `ip_address`, `user_agent`
- `Grievance.category` choices: `content`, `privacy`, `account`, `payment`, `other`
- `Grievance.status` choices: `open`, `in_progress`, `resolved`, `rejected`, `escalated`
- Added `AuditLog` model (lines 297-320) — CERT-In 180-day log retention: `user`, `action`, `resource_type`, `resource_id`, `metadata`, `ip_address`, `user_agent`, `created_at`
- `AuditLog` indexes on `(user, -created_at)` and `(resource_type, resource_id)`

**Settings (`backend/EchoFlow/settings.py`):**
- Env-driven regulatory contacts (lines 643-657): `COMPLIANCE_OFFICER_EMAIL`, `GRIEVANCE_OFFICER_EMAIL`, `NODAL_CONTACT_EMAIL` (with defaults)
- `TERMS_VERSIONS` env var (comma-separated) for consent versioning
- `AWS_S3_REGION_NAME` assertion for `ap-south-1` / `ap-south-2` (DPDP + RBI)

**Views (`backend/app/views/data_subject.py`):**
- `/legal/compliance/` — returns officer contacts (IT Rules 4(1)(a)(b)(c))
- `/auth/consent/withdraw/` — sets `ConsentAudit.withdrawn_at`, triggers 30-day cooling-off soft-delete (DPDP §14)
- `/auth/data/export/` — DPDP §14 data portability: exports all user data as JSON
- `/auth/data/delete/` — DPDP §14 right to erasure with CERT-In retention override

**Tests (`backend/app/tests/test_system_health.py`, `test_auth_regulatory.py`):**
- Grievance endpoint validation
- Compliance endpoint requires auth + returns JSON

### Phase C — Content Moderation Pipeline (COMPLETED)

**Services (`backend/app/services/content_moderation.py`):**
- v1 offline moderation: `sha256` fingerprint of normalized file + blocked-phrase list against lowercase transcript + AI tags
- `AudioClip.moderation_approved` boolean gate (models.py:113) — HLS generation only runs when True
- `process_audio_to_hls` task checks `moderation_approved` before processing
- `FINGERPRINT_BLOCKLIST` module-level set (TODO: move to Redis for production)

**Uploads (`backend/app/services/uploads.py`):**
- `trigger_hls_processing` enqueues task only after moderation approval
- `finalize_upload` no longer enqueues HLS task (flow changed)

### CERT-In 180-Day Log Retention (COMPLETED)

**Models (`backend/app/models.py`):**
- `AuditLog` with `identity_retained_until = created_at + 180 days` (CERT-In §5(1))
- `ConsentAudit.identity_retained_until = consent_issued_at + 180 days`
- `Grievance` retains user identity for 180 days post-resolution

**Middleware (`backend/app/middleware.py`):**
- Request/response audit logging (lines 292-293) — DB write overhead accepted for audit trail
- Correlation ID propagation for cross-service tracing

### S3 Region Enforcement (COMPLETED)

**Settings (`backend/EchoFlow/settings.py`):**
- `STORAGES["default"]["OPTIONS"]["region_name"]` asserted to `ap-south-1` / `ap-south-2` / `auto` (lines 492-498)
- Signed S3 URLs instead of public bucket (lines 467-479)

### Environment Variables Required (see above)

| Variable | Purpose | Default |
|---|---|---|
| `TERMS_VERSIONS` | Comma-separated consent versions (e.g. `v1.0,v1.1`) | `v1.0` |
| `COMPLIANCE_OFFICER_EMAIL` | CCO email (IT Rules 4(1)(b)) | `compliance@echoflow.in` |
| `GRIEVANCE_OFFICER_EMAIL` | Grievance email (IT Rules 4(1)(a)) | `grievance@echoflow.in` |
| `NODAL_CONTACT_EMAIL` | Nodal contact email (IT Rules 4(1)(c)) | `nodal@echoflow.in` |
| `AWS_S3_REGION_NAME` | **Must be `ap-south-1` or `ap-south-2`** for DPDP/RBI | `auto` (prod must override) |
| `PHYSICAL_ADDRESS` | Registered office (IT Rules / Consumer Protection) | Not yet exposed |

### Remaining Gaps (Open)

- Public clip endpoint needs `moderation_approved` filter (TODO in `docs/INDIA-REGULATORY-READINESS.md`)
- Multilingual India-specific prohibited-content database to replace blocked phrase list (TODO in `services/content_moderation.py:19-20`)
- Transcript text persistence from `process_audio_to_hls` task (TODO in `services/content_moderation.py:167-175`)
- Takedown workflow endpoint (`POST /clips/{id}/takedown/`)
- `pydub` temp-file stream for memory pressure (TODO in `serializers.py:250`)

## HTTPS / TLS Termination
The stack now ships with an nginx reverse proxy in front of every other service. TLS is terminated at the edge; internal hops (nginx→gunicorn, nginx→minio) stay plain HTTP on the docker bridge. No application code knows TLS exists.

| Concern | Where it lives | Notes |
|---|---|---|
| Public-facing entrypoint | `docker-compose.yml:nginx` (image `nginx:1.27-alpine`) | Three listeners: `:80` (HTTP→HTTPS redirect), `:443` (Django), `:9443` (MinIO for browser HLS). |
| TLS cert + key | `docker/certs/localhost.{crt,key}` (self-signed dev) | Bind-mounted read-only into nginx; never enters the app image. Production swaps in Let's Encrypt material via the same path. |
| TLS config | `docker/nginx.conf` | TLS 1.2/1.3 only, HSTS 1y+includeSubDomains+preload, `X-Forwarded-Proto https` on every upstream block. |
| Django TLS contract | `backend/EchoFlow/settings.py:529-539` `if not DEBUG:` block | `SECURE_SSL_REDIRECT=True`, `SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https')`, `SESSION/CSRF_COOKIE_SECURE=True`, HSTS 1 year. **Requires `DJANGO_DEBUG=False`** — set this in `.env` before going anywhere public. |
| In-container healthcheck | `Dockerfile` + `docker-compose.yml` (web service) | Sends `X-Forwarded-Proto: https` so `SECURE_SSL_REDIRECT` doesn't loop the in-container probe. |
| Test coverage | `backend/app/tests/test_https_termination.py` (32 tests) | Cert, nginx config, prod settings, proxy header, public media endpoint, live terminator. |

**Operating rules:**
- **Do NOT change `SECURE_PROXY_SSL_HEADER` to anything other than `('HTTP_X_FORWARDED_PROTO', 'https')`** without also updating every `proxy_set_header X-Forwarded-Proto https;` line in `docker/nginx.conf`. Mismatch = redirect loop or insecure cookies.
- **The `8005:8000` host port mapping on the `web` service is a debug escape hatch**, not the supported path. Attackers on the same network can hit gunicorn directly and spoof `X-Forwarded-Proto: https` to themselves. In prod, drop that port mapping entirely.
- **Cert rotation is `docker compose exec nginx nginx -s reload`** — no app rebuild, no container restart. The bind-mount picks up the new files.
- **Full design + production-readiness checklist:** `docs/EXPLAIN/docker/05-https-tls-termination.md` and `docs/EXPLAIN/docker/06-https-production-readiness.md`.

## API Endpoints
```
POST /auth/register/          # Register (public)
POST /auth/login/             # JWT obtain pair
POST /auth/token/refresh/     # JWT refresh

POST /clips/                  # Upload audio (auth) → triggers Celery `process_audio_to_hls`
GET  /feed/                   # Redis-backed personalized feed (auth)
POST /interactions/{id}/toggle-like/
POST /interactions/{id}/register-skip/
POST /interactions/{id}/log-telemetry/
GET  /comments/?clip={id}     # Filter by clip
POST /share/{id}/send-share/
GET  /follow/{id}/toggle-follow/
POST /tags/initialize/        # Cold-start: bootstrap user vectors from tags
GET  /suggestions/?category=X # Category-scoped vector ranking
GET  /profile/me/             # Own profile
GET  /profile/{id}/           # Public profile
```

## Architecture Notes
- **Dual `EchoFlow/`**: Project package (`backend/EchoFlow/settings.py`, `urls.py`, `celery.py`) vs app package (`backend/app/`). Don't confuse them.
- **Custom user model**: `backend.app.User` (extends `AbstractUser`). Set via `AUTH_USER_MODEL = 'backend.app.User'`.
- **Recommendation engine**: Composite scoring = 45% vector similarity + 30% avg completion rate + 25% engagement velocity. 80% exploit / 20% explore feed mixing.
- **Redis feed queues**: Per-user `user_feed:{id}` lists. `FastFeedViewSet` pops 10 at a time; refills trigger when queue < 15.
- **Vector fields**: `semantic_vector` (384-dim, from transcript via sentence-transformers), `acoustic_vector` (128-dim, from librosa). HNSW indexes (`m=16, ef_construction=64`) on both.
- **Celery task routing**: `process_audio_to_hls` → `heavy_media` queue; `refill_user_feed` → `fast_feed` queue; `cleanup_orphan_hls` → 03:00 UTC daily; `flush_counters_to_pg` → every 300s (defined in `backend/EchoFlow/settings.py` `CELERY_TASK_ROUTES` + `CELERY_BEAT_SCHEDULE`).
- **ML models lazy-loaded**: `get_whisper_model()`, `get_embedding_model()`, `get_kw_model()` in `backend/app/tasks.py` — initialized on first task call, not at import time.
- **`update_global_metrics`** is a no-op stub (deprecated 2026-09). All three responsibilities (counter deltas, avg_completion_rate, engagement_velocity) live in `flush_counters_to_pg`. The Celery Beat entry is kept for one cycle so a missing task name surfaces as a deployment error.
- **Event-driven metrics pipeline**: user interactions (`record_like_toggle`, `record_skip`, `record_share`, `record_telemetry` Tier-3 fallback) write to Redis via `counter_store.increment` / `add_completion` (O(1) on the request path). `flush_counters_to_pg` (every 5 min) drains the deltas and applies them to Postgres in batched UPDATEs that touch only the dirty clip set. No correlated subquery, no full-table scan. See [docs/EXPLAIN/decisions/event-driven-metrics.md](docs/EXPLAIN/decisions/event-driven-metrics.md).
- **Read replica routing**: `backend/app/db_routers.py` is a 71-line `ReadRouter` with 4 hooks (db_for_read/db_for_write/allow_relation/allow_migrate). Auto-activates when `READ_DATABASE_URL` is set; the `if not atomic and not SELECT FOR UPDATE` guard prevents stale-read races inside write transactions. See [docs/EXPLAIN/database/05-read-replica-design.md](docs/EXPLAIN/database/05-read-replica-design.md).
- **Per-session DB timeouts**: `backend/EchoFlow/settings.py` sets `statement_timeout=30s`, `idle_in_transaction_session_timeout=60s`, `lock_timeout=10s`, `connect_timeout=10s` on the default connection via libpq `options` string. Critical behind PgBouncer (25-conn pool); a slow query that held a backend connection could otherwise exhaust the pool. Gated on `ENGINE.endswith('postgresql')` so non-Postgres backends are unaffected.
- **Cache invalidation**: `services/interactions.py::invalidate_user_vectors_cache` is called from `record_like_toggle`, `record_skip`, `record_share`, and `record_telemetry`'s sync fallback via `transaction.on_commit`. The `flush_telemetry_stream` consumer invalidates each unique user's cache after a successful `bulk_create`. Stale-vector window collapsed from 15 min to near-zero for all user-state-mutating paths.
- **Counter store (event-driven, no dual-write)**: `services/counter_store.py` writes user-engagement counters to Redis (`INCRBY` for likes/shares/skips; `INCRBYFLOAT` + `INCR` for per-(user,clip) completion). The `UserInteraction.save()` F() side-effect was removed in the 2026-09 metrics rewrite; `flush_counters_to_pg` is the only path from Redis to Postgres. `ECHOFLOW_DUAL_WRITE_COUNTERS` is now a no-op (always False) and slated for deletion. See [docs/EXPLAIN/decisions/event-driven-metrics.md](docs/EXPLAIN/decisions/event-driven-metrics.md).
- **HLS output**: Stored under `media/hls/{clip_id}/` on local disk. Not S3-backed yet. `cleanup_orphan_hls` Celery task (daily 03:00 UTC) prunes directories older than 1 day that are not in the `AudioClip` table — bounded to 1000 keys/run.

## Scraping / Ingestion
```bash
# Management command
python manage.py scrape_audio --source=wikimedia --limit=3 --clip-length=30

# Celery task
python -c "from backend.app.tasks import scrape_and_import; scrape_and_import.delay('internet_archive', limit=5)"
```
Sources: wikimedia, internet_archive, freesound (needs `FREESOUND_API_KEY`), kaggle (needs `SCRAPER_KAGGLE_LOCAL_PATH`). Respects `robots.txt`. Allowed licenses configurable via `SCRAPER_ALLOW_LICENSES`. Source connectors live in `ai_ml/scrapers/sources/`; the `scrape_audio` management command + `scrape_and_import` Celery task remain in `backend/app/`.

## Frontend (sample only)
```bash
cd frontend
npm install
npm run dev      # Vite dev server on port 5173
npm run build
```
Uses HLS.js for playback. This is an example client — the production frontend may differ.

## Testing & Linting
- Test framework: **pytest** + `pytest-django`, installed in the `api` image. Run via `docker compose exec web pytest …` — see [Running Tests](#running-tests) for the full command set.
- Test files live under `backend/app/tests/` (24 files: `test_adversarial_pass3.py`, `test_auth_regulatory.py`, `test_counter_store.py`, `test_db_router.py`, `test_feed_pool.py`, `test_hls_token.py`, `test_https_termination.py`, `test_integration_concurrency.py`, `test_integration_pgvector.py`, `test_metrics_endpoint.py`, `test_metrics.py`, `test_observability_tui.py`, `test_orphan_cleanup.py`, `test_scraper.py`, `test_security_and_validation.py`, `test_sentry.py`, `test_services_comments.py`, `test_services_follows.py`, `test_services_interactions.py`, `test_services_shares.py`, `test_services_uploads.py`, `test_settings.py`, `test_smoke.py`, `test_system_health.py`, `test_task_publisher.py`).
- All tests run against PostgreSQL in Docker. No SQLite fallback.
- No linting/formatter config (no `.eslintrc` at root, no `pyproject.toml`, no `ruff.toml`).
- CI: `.github/workflows/django.yml` runs migrations + the test suite via Docker. Blocks merges on failure.
- **Current count: 275 passed, 6 skipped, 0 failed.** Skipped = 1 ffmpeg-environmental (`test_scraper.py::test_normalizer_trims_to_max_seconds` is conditionally skipped when ffmpeg is missing on the host) + 5 live-nginx-environmental (`TestLiveNginxTerminator` requires the full `docker compose up` stack). The 6th previously-running test, `test_scraper.py::test_uploader_creates_audioclip`, was the only one in that group that ever ran in a previous configuration; it now passes after the import fix (see "Recent fixes" below).
- **Root cause of 178 `auth_group does not exist` errors:** The old conftest.py used a SQLite override hack that bypassed real migrations. The fix was to make Docker/Postgres the only test environment. The new `conftest.py` auto-creates `echoflow_test` DB, installs pgvector on `template1`, and handles session teardown.
- **docker-compose.test.yml** — test-only stack (db, redis, minio, web). No nginx, no celery workers. Run with: `docker compose -f docker-compose.yml -f docker-compose.test.yml up --build -d` then `docker compose exec -e PYTHONPATH=/app web pytest backend/app/tests/ --tb=short`.
- **Recent fixes (2026-09-07):**
  - **`backend/app/migrations/0002_audioclip_cover_image.py`** (added) — the `AudioClip.cover_image` field was added to the model (line 85) but the migration was never generated, so every `INSERT INTO app_audioclip` failed with `column "cover_image" of relation "app_audioclip" does not exist`. The migration was generated by `manage.py makemigrations` and added to fix 73 cascading fixture-setup errors across `test_adversarial_pass3.py`, `test_counter_store.py`, `test_orphan_cleanup.py`, `test_security_and_validation.py`, `test_services_{comments,interactions,shares,uploads}.py`, `test_task_publisher.py`, and `test_integration_{concurrency,pgvector}.py`.
  - **`ai_ml/scrapers/uploader.py:17`** (fixed) — was `from ..models import AudioClip` (a relative import left over from when the scraper lived at `backend/app/scrapers/uploader.py`); changed to the absolute `from backend.app.models import AudioClip` to match the pattern used by every other `ai_ml/` file. Was causing `ImportError: cannot import name 'AudioClip' from 'ai_ml.models'` in `test_scraper.py::test_uploader_creates_audioclip`.
  - **`docker/postgres-init/`** (new directory) — three init SQL scripts that run on the main `db` service's first startup: `00-init-pgvector.sql` installs the extension in `POSTGRES_DB` (echoflow_db) so Django migrations can find it; `01-init-pgvector-template1.sql` runs `\c template1` then installs the extension on the template (CRITICAL — must run after `00-` so `template1` has vector before `02-` runs); `02-echoflow-test-db.sql` runs `CREATE DATABASE echoflow_test OWNER echoflow` (idempotent via `\gexec` + `WHERE NOT EXISTS` guard). Filename ordering is load-bearing — see "Postgres init scripts" below.
  - **`docker-compose.yml:11-22`** (modified) — the `db` service now mounts `./docker/postgres-init` (instead of just the old `docker/test/postgres-init/init-pgvector.sql` single file) at `/docker-entrypoint-initdb.d:ro`. The single-file mount only installed vector in `echoflow_db`; the directory mount provisions both `echoflow_db` (main) and `echoflow_test` (dev) with pgvector on a fresh data volume. The separate `docker-compose.test.yml` still uses `./docker/test/postgres-init` for its own dedicated test-db container (clean isolation from dev data).
- **Postgres init scripts:** the main `db` service runs `docker/postgres-init/*.sql` in alphabetical order on first startup of a fresh data volume. The load-bearing order is `00-` (default DB) → `01-` (template1) → `02-` (create test db). If you change a filename, re-read the dependency comments in each file or you will silently break `CREATE DATABASE` for `echoflow_test` (vector extension is required on the source template). Wipe the volume (`docker volume rm echoflow_postgres_data`) if you change an init script — init scripts only run on a fresh data directory.
- **HNSW index EXPLAIN test gotcha:** `SET LOCAL enable_seqscan = OFF` requires an active transaction. Wrap it in `transaction.atomic()` to ensure it takes effect. Also verify the index type via `pg_am.amname` as a primary check (not just the EXPLAIN plan, which may choose Seq Scan for small tables).
- **S3 storage in tests:** Use `default_storage.exists(clip.original_file.name)` instead of `os.path.exists(clip.original_file.path)` — `.path` raises `NotImplementedError` on S3 storage backends (MinIO).
- **Conditional skip pattern for system binaries:** Use `@unittest.skipUnless(_ffmpeg_available, "requires ffmpeg on PATH")` where `_ffmpeg_available = shutil.which('ffmpeg') is not None`. This passes in Docker (ffmpeg installed) and skips on bare-metal dev.
- **F() expressions for atomic updates in concurrency tests:** Use `F('likes') + 1` instead of read-modify-write patterns (`obj.likes = obj.likes + 1`) to avoid race conditions.
- **date() objects for DOB fields:** Use `date(1990, 1, 1)` instead of string literals for date fields to avoid type errors.
- **trigger_hls_processing vs finalize_upload:** The upload flow changed; use `trigger_hls_processing` instead of the old `finalize_upload` in test fixtures.
- **cache import in adversarial tests:** Some test files need `from django.core.cache import cache` to work with Django's test cache backend.
- **postgresql assertion in smoke tests:** Changed from `sqlite3` to `postgresql` in smoke test assertions to match the Docker-only test environment.

### Known Skipped / Disabled Tests (environmental, not regressions)

The following test is **conditionally skipped** with `@unittest.skipUnless(_ffmpeg_available, ...)` because it requires `ffmpeg` on `PATH`. The `api` Docker image already installs ffmpeg (in the `base` stage of the Dockerfile), so this test **passes in Docker**. If running on a bare-metal dev machine without ffmpeg, it will be skipped:

| Test | Reason | How to enable locally |
|------|--------|------------------------|
| `backend/app/tests/test_scraper.py::ScraperUnitTests::test_normalizer_trims_to_max_seconds` | Requires `ffmpeg` on `PATH` (used by `pydub` for MP3 export) | `sudo apt install ffmpeg` (Debian/Ubuntu/Pop!_OS) or `brew install ffmpeg` (macOS) |

> The second scraper test, `test_uploader_creates_audioclip`, was previously ffmpeg-conditional too but now runs (it was failing with `ImportError` due to a broken relative import; the import was fixed in 2026-09 — see "Recent fixes" below).

The following nginx HTTPS termination tests require the `nginx` container (not part of the test stack):

| Test | Reason |
|------|--------|
| `backend/app/tests/test_https_termination.py::TestNginxConfig::test_nginx_parses_with_no_errors` | Requires `nginx` on `PATH` (only in the full Docker stack) |
| `backend/app/tests/test_https_termination.py::TestLiveNginxTerminator::*` | Live HTTP/HTTPS requests against nginx (not in test stack) |

These use `@unittest.skip(...)` for nginx (binary not available) and the full `docker compose up` stack for the live terminator tests. They pass when running the full stack.

If you add a test that needs a system binary not present in the Docker image, follow the same pattern: `@unittest.skip("requires <binary> on PATH; see AGENTS.md")`.

**Do NOT** comment-out or remove tests that fail for reasons you don't understand. If a test fails and the cause is unclear, debug it: run with `pytest --tb=long`, read the traceback, search the codebase for the operation being tested, and check whether the test environment matches the AGENTS.md prerequisites (Python 3.11, Postgres 16, Redis 7, FFmpeg on `PATH`, `docker compose` running). Only after you understand WHY a test fails — and the cause is environmental, not a code bug — should you add a skip with a clear reason.

### Local `.env` discipline

- `.env` is **gitignored**. Do not commit it. The boilerplate is `.env.example`, `.env.vps.example`, and `.env.laptop.example`; copy one of those to `.env` and edit locally. The `.gitignore` allows committing `*.example` files (see `.gitignore` exception rules for `.env.*.example`).
- Tracked env files must have `DJANGO_DEBUG=False`. CI runs `scripts/check_no_tracked_env.sh` on every PR; a tracked env file with `DJANGO_DEBUG=True` will block the merge.
- `HF_TOKEN` and `DJANGO_SECRET_KEY` in your local `.env` are real secrets. If you accidentally commit them, rotate them immediately.

### `AGENTS.md` is tracked

`AGENTS.md` (this file) is checked into the repository and is the canonical quick-start for new coding agents. Update it whenever you:
- add or change a required env var,
- change the test command (e.g., new PYTHONPATH requirement),
- move a major subsystem (e.g., a Celery task, a service, a queue),
- discover a gotcha that the next agent will hit.

Keep changes minimal and additive — the file is read on every session. Don't add code snippets longer than ~10 lines; link to docs instead.

## Gotchas
- `DEBUG = True` is hardcoded in `backend/EchoFlow/settings.py:15` — env-driven override exists (`DJANGO_DEBUG=False`). **MUST be `False` once the nginx terminator is live**, otherwise `SECURE_SSL_REDIRECT` 301-loops on the in-container `/health/` probe (the in-container healthcheck now sends `X-Forwarded-Proto: https` to compensate; the regression test `test_in_container_healthcheck_must_send_forwarded_proto` enforces this).
- `ALLOWED_HOSTS` is env-driven (`DJANGO_ALLOWED_HOSTS=localhost`). Add your host IP if accessing via LAN/Tailscale.
- `CORS_ALLOW_ALL_ORIGINS = True` in settings.py is hardcoded — env override (`DJANGO_CORS_ALL`) exists but the code sets it to True after the env check. With the terminator live, leave `DJANGO_CORS_ALL=False` and enumerate `https://...` origins explicitly.
- `requirements.txt` lists `librosa` twice (lines 8 and 28) — harmless but sloppy.
- `backend/scripts/seed_db.py` targets port 8005 (Docker) not 8000 (dev server). Adjust `API_ENDPOINT` if running locally. After the terminator: `https://localhost/clips/`.
- `backend/wait_for_db.py` polls the database with exponential backoff (up to 120
  attempts); relies on `DATABASE_URL` being set and resolvable in the
  Compose network.
- `process_audio_to_hls` is enqueued via `transaction.on_commit` in `backend/app/views.py:112` — won't fire if the transaction rolls back.
- Comment count on `AudioClip` is denormalized and updated in `Comment.save()/delete()` — not via signals.
- `UserInteraction` uses `F()` expressions for atomic counter increments on likes/shares/skips.
- **Self-signed dev cert (`docker/certs/localhost.crt`) is in the repo on purpose** so a fresh clone works. For prod, replace with Let's Encrypt material and `nginx -s reload` — the cert is bind-mounted, so no rebuild is needed. **Do NOT push the dev key to a public registry in any fork that re-publishes the image**; revocation is the only fix.
- **HLS token cookies**: The `ef_hls_token` cookie must have `SameSite=Lax` (not `Strict`) so it's sent on top-level navigation from `app.echo-flow.in` to `media.echo-flow.in` (SameSite=Lax permits cookies on same-site top-level navigations, but blocks cross-site). `Secure` requires HTTPS on both `api.echo-flow.in` and `media.echo-flow.in`. In dev, `Domain` attribute must be empty (localhost doesn't support domain cookies). See `docs/EXPLAIN/storage/04-hls-token-protection.md`.
- **HLS token secret sync**: In production, `MEDIA_TOKEN_SECRET` must be **identical** in the VPS `.env` (Django issuance) and the Cloudflare Worker secret (`npx wrangler secret put MEDIA_TOKEN_SECRET`). If these diverge, all HLS playback returns 403.
- **RFC 3986 §5.2.2 — Signed URLs don't work for HLS**: The master playlist references variant playlists and segments via relative paths. RFC 3986 §5.2.2 strips query strings during relative-reference resolution, so signed URLs (which rely on query parameters) fail on the second and subsequent HLS requests. **Signed cookies are the only viable token mechanism for HLS.** This applies to any multi-file streaming protocol (HLS, DASH, Smooth Streaming).
- **fetch `credentials: 'include'` for Set-Cookie**: When using `fetch()` to call an endpoint that sets an HttpOnly cookie via `Set-Cookie`, the fetch request **must** include `credentials: 'include'` (or `'same-origin'`). Without it, the browser silently discards the Set-Cookie header. This is a common gotcha when building token-issuance endpoints.
- **HLS token endpoint returns Set-Cookie, not JSON body**: The `/media/playback-token/<clip_id>/` endpoint sets the token as a cookie and returns `{"status": "ok"}`. The frontend must NOT read the token from the response body — it's set as an HttpOnly cookie and auto-sent by the browser on all `/hls/*` requests.

## Docs
- `docs/backend-architecture-audit.md` — production scaling analysis (S3, PgBouncer, Kafka, etc.)
- `docs/scaling-analysis.md` — capacity planning notes
- `Startup_related_docs/` — market research and planning docs
- `docs/backend-bug-fixs.md` — audit + Group A/B/C/D/partial-issues fix reports (4 parts)
- `docs/EXPLAIN/decisions/partial-issues-completion-plan.md` — plan + completion record for the 7 partially-addressed items (A1, A3, A5, A8, B13, B14, B17) + B19 docstring
- `docs/EXPLAIN/decisions/group-b-architectural-plan.md` — plan for Group B items 9-12
- `docs/EXPLAIN/operations/hf-token-rotation.md` — HF_TOKEN rotation runbook (B17)
- `docs/EXPLAIN/observability/04-prometheus-grafana-setup.md` — Prometheus + Grafana activation (A8)
- `docs/EXPLAIN/database/05-read-replica-design.md` — read-replica design + activation playbook (A5)
- `docs/EXPLAIN/DEPLOYMENT/` — Hybrid deployment documentation (VPS + laptop + Cloudflare R2 + Tailscale)
- `docs/EXPLAIN/storage/04-hls-token-protection.md` — Short-lived HLS play token design (signed cookies + Cloudflare Worker / nginx njs)

---

## Engineering Principles

The following principles govern all code changes. These are standard SWE best practices distilled from the full protocol:

**Truth Protocol:** Source code > migrations > tests > config > docs > comments. Never invent behavior to reconcile conflicts; investigate instead.

**Golden Rules:**
- Understand before changing; root-cause fixes over symptoms
- Minimal viable changes; no "while I'm here" cleanup
- Never add a dependency without checking it exists in the repo first
- Tests are part of the implementation; don't delete/weaken/skip without justification
- Get approval before architecture/API/schema/security/deployment changes
- Design for failure: retries, idempotency, race conditions, partial completion

**Git Safety:** Use a dedicated branch. Never `git reset --hard`, `git clean`, or `git push --force` without explicit authorization.

**Documentation:** Put repo-specific notes in `docs/EXPLAIN/`. Use `DECISION:` / `SECURITY:` / `HACK:` / `TODO:` tags in code.

**For complex tasks touching approval gates (architecture, APIs, schemas, auth, security, deployment):**
Produce a design doc at `docs/EXPLAIN/decisions/YYYY-MM-DD-<slug>.md` with Changes Needed, How Changes Will Be Made, Why This & Not Anything Else, Files Affected, Architecture & Data Flow, Test Cases, Edge Cases, Atomic Commit Plan. Get explicit approval before implementing.

## Distributed-System & Security Reminders

When changing distributed workflows (Django, PostgreSQL/pgvector, Redis, Celery, MinIO/S3, FFmpeg/HLS, ML workers), consider: duplicate execution, retries, idempotency, race conditions, ordering, stale data, worker failure, process restart, partial completion, timeouts, resource exhaustion, network failure.

Never assume a task runs exactly once unless the system guarantees it. For every retryable operation, ask whether repeating it is safe.

**Media and Storage Invariants:** original uploads live in object storage; HLS output is generated in local worker scratch space then uploaded; containers must not assume a shared filesystem; HLS playback uses token-gated `hls/` paths (signed cookies); original `uploads/` remain private (signed S3 URLs); local scratch files must be cleaned up after processing. Verify in `settings.py`, `media_urls.py`, `tasks.py`, `docker-compose.yml` before modifying.

**Security:** Never hardcode secrets. Treat all input as untrusted. Validate/sanitize at boundaries. Before changing security-sensitive code, consider auth, authorization, injection, SSRF, path traversal, command execution, secret leakage, sensitive-data exposure, race conditions. For DB changes, inspect migrations, existing data, locking, rollback, compatibility.

## Session Learnings & Known Things

**This section accumulates durable, repo-specific knowledge across sessions.** Every session that touches non-trivial code **must** append an entry before ending.

**Format:**
```markdown
### YYYY-MM-DD — <short feature/fix slug>

**Context:** What was the task.

**What Was Learned (Durable):** Key repo facts, architecture, failure modes, configs, dependencies, test gaps — don't make the next agent rediscover these.

**What Changed:** Files modified, migrations added, tests added.

**Open Questions / Unresolved Risks:** Things not fully verified.

**Design Doc Reference:** (if applicable)
```

---

### 2026-09-07 — media-image-build + test-suite-greening + db-init-rewiring

**Context:** Three separate but related fixes to unblock the media image build, get the test suite green, and let the main `db` service host both `echoflow_db` and a developer `echoflow_test` database.

**What Was Learned (Durable):**
- **BuildKit `--mount=type=cache` is ephemeral.** Files written to a cache target during a `RUN` exist in a temporary overlay and are saved to the BuildKit cache store, but are NOT part of the committed layer's filesystem. A subsequent `COPY --from=<stage> <cache-mount-path>` in another stage fails with `not found` for that path. **DECISION:** after the model download, `cp -a /home/appuser/.cache/huggingface /home/appuser/hf_baked` in `py-deps-media`, then `COPY --from=py-deps-media /home/appuser/hf_baked /home/appuser/.cache/huggingface` in `media`. The cache mount still gives cross-build speed (saves the ~5 min / 250 MB download on subsequent builds); the `cp -a` only runs on the build host, not in the shipped layer.
- **`cover_image` migration gap (model vs migration drift).** `AudioClip.cover_image = models.ImageField(...)` was added to the model (`backend/app/models.py:85`) without a corresponding `migrations/0002_audioclip_cover_image.py`. Every `INSERT INTO app_audioclip` failed with `column "cover_image" of relation "app_audioclip" does not exist`. Because the repo-root `conftest.py` fixtures (`ready_clip`, `processing_clip`) call `AudioClip.objects.create(...)`, the failure cascaded to **71 fixture-setup ERRORs** across 9 test files (not actual assertion failures). **DECISION:** the fix is `manage.py makemigrations` — additive, nullable, no default backfill needed. **HACK:** no CI guard against this drift; a `manage.py makemigrations --check --dry-run` step in `.github/workflows/django.yml` would catch it. (Not yet wired.)
- **`from ..models import AudioClip` is a relative import leftover.** After the scraper moved from `backend/app/scrapers/` to `ai_ml/scrapers/` (commit `b4f749d`), `ai_ml/scrapers/uploader.py:17`'s `from ..models import AudioClip` resolved to `ai_ml.models` (which only has ML wrappers). **DECISION:** every `ai_ml/` file that needs the Django ORM uses the absolute `from backend.app.models import AudioClip` (see `ai_ml/pipelines/recommendation.py:195` for the canonical example) — uploader now matches.
- **Postgres entrypoint init-script scoping trap.** The official `postgres` image runs each `*.sql` script in `/docker-entrypoint-initdb.d/` against `POSTGRES_DB` (the default DB), NOT against `template1`. So `CREATE EXTENSION IF NOT EXISTS vector;` only installs in the default DB, not in `template1`, breaking any subsequent `CREATE DATABASE` (which copies `template1`, not the default DB). **DECISION:** install vector in the default DB first (`00-init-pgvector.sql`), then `\c template1` and install again (`01-init-pgvector-template1.sql`), then `CREATE DATABASE` (`02-echoflow-test-db.sql`). Filename alphabetical order is load-bearing. **SECURITY:** without the `\c template1`, the test DB silently lacks vector and the test suite fails with `type "vector" does not exist` on the first INSERT into a `VectorField`.
- **Init scripts only run on a fresh data volume.** Existing `echoflow_postgres_data` volumes have already been initialized, so mounting a new init script into a running container has no effect. To pick up new scripts, `docker compose down && docker volume rm echoflow_postgres_data && docker compose up -d`.
- **Test isolation bug in `TestLiveNginxTerminator`.** The `skip_if_nginx_not_reachable` autouse fixture (line 671 of `test_https_termination.py`) does `socket.create_connection(('nginx', 443), timeout=1)` and only skips if the connection FAILS. If the main stack's `nginx` is up while the test stack runs (same `echoflow_default` network), the test does NOT skip — it runs and gets HTTP 502 because the upstream is the main `web`, not the test `web`. **HACK:** workaround is `docker compose stop nginx` before running tests, or run the test suite on a host that doesn't have the main stack running. A proper fix would also probe the response body, not just the TCP socket. (Tracked separately.)
- **Local `.env` discipline:** the developer's `.env` had `DATABASE_URL=.../echoflow_test` (intentional, to use the dev test db), but the main `db` service only creates `echoflow_db` from `POSTGRES_DB`. The init script `02-echoflow-test-db.sql` now provisions `echoflow_test` inside the main `db` so the URL resolves correctly without needing a separate `docker-compose.test.yml` for local dev. The test stack still uses its own dedicated `echoflow_test` container for the pytest suite (clean isolation).

**What Changed:**
- `Dockerfile:165-175` — added `cp -a /home/appuser/.cache/huggingface /home/appuser/hf_baked` after the model download commands in `py-deps-media`. Inline `DECISION:` comment explains the constraint.
- `Dockerfile:232-238` — `media` stage now `COPY --from=py-deps-media /home/appuser/hf_baked /home/appuser/.cache/huggingface` (was the cache-mount path; that's the bug).
- `backend/app/migrations/0002_audioclip_cover_image.py` (new) — auto-generated, adds `cover_image` column.
- `ai_ml/scrapers/uploader.py:16-26` — `from ..models import AudioClip` → `from backend.app.models import AudioClip` + DECISION comment.
- `docker/postgres-init/00-init-pgvector.sql` (new) — installs vector in default DB.
- `docker/postgres-init/01-init-pgvector-template1.sql` (new) — `\c template1` then installs vector.
- `docker/postgres-init/02-echoflow-test-db.sql` (new) — idempotently creates `echoflow_test`.
- `docker-compose.yml:11-22` — `db` service now mounts `./docker/postgres-init` directory (was a single file).
- `.github/workflows/docker-image.yml` — added per-target layer cache scope, per-matrix `load: true` for PR smoke tests, and a media-image smoke test that loads the baked HF models in offline mode (catches the `cp -a` regression).
- `AGENTS.md` "Recent fixes" section + "Postgres init scripts" notes (above).
- `README.md` "Testing" section — test count + new test-isolation caveat.

**Open Questions / Unresolved Risks:**
- The `TestLiveNginxTerminator` 4-test false-failure when the main stack is up is unfixed. Either change the fixture to probe the response body, or run test suites in a CI-only environment.
- The CI guard `manage.py makemigrations --check --dry-run` is recommended but not yet wired into `.github/workflows/django.yml`. Adding it would prevent this class of bug.
- The wheelhouse regen script in this file does NOT include `sentry-sdk[django,celery]==2.18.0` — actually it does (the regen doc was updated for it; this risk is closed).

**Design Doc Reference:** None (these were bug fixes; no design doc was produced). The test-fix decision tree and Dockerfile `cp -a` rationale are captured inline in the file `DECISION:` comments.