import os
from pathlib import Path
import dj_database_url
from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured
# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
# DECISION: Fail fast on missing DJANGO_SECRET_KEY. Generating a random
# key per process would silently break session/CSRF/signature
# verification across the gunicorn + Celery fleet — every worker would
# have a different key.
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY is not set. Application cannot start without it."
    )

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DJANGO_DEBUG', 'False').lower() == 'true'

ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost').split(',')
CORS_ALLOWED_ORIGINS = os.environ.get('DJANGO_CORS_ALLOWED_ORIGINS', 'http://localhost:3000,http://localhost:5173,http://localhost:3021').split(',')
# DECISION: CORS_ALLOW_ALL_ORIGINS is hard-coded to False; the env-driven
# allowlist above is the single source of truth. Previously this line was
# read from DJANGO_CORS_ALL env var but then unconditionally reassigned
# to False on line 63, making the env var dead code. Removed for clarity.
CORS_ALLOW_ALL_ORIGINS = False

# N14 fix: CORS_URLS_REGEX was r'^.*$' which sent CORS headers to
# every URL (including /admin/, /auth/, /metrics/). The actual security
# boundary is CORS_ALLOWED_ORIGINS, but the wide regex serves no
# purpose. The /media/ Django route was removed when S3Storage was
# adopted (per docs/stateful-media-storage-at-scale.md and
# media_urls.py:18-37 — playback URLs come from signed S3 URLs, not
# from a Django route). Set to an empty regex (never matches) so
# django-cors-headers never applies CORS via the regex path. The
# middleware will still apply CORS_ALLOWED_ORIGINS to all responses
# that flow through its check_origin method.
CORS_URLS_REGEX = r'$.^'  # matches nothing (negative lookahead on start)

CORS_ALLOW_METHODS = [
    'GET',
    'POST',
    'PUT',
    'PATCH',
    'DELETE',
    'OPTIONS',  # required for preflight requests
]
CELERY_BROKER_HEARTBEAT = 120 # Increase to 2 minutes
CELERY_BROKER_HEARTBEAT_CHECKRATE = 2

CORS_ALLOW_HEADERS = [
    'accept',
    'authorization',
    'content-type',
    'origin',
    'range',   # ← critical for HLS: browsers send Range headers for partial content
]

CORS_EXPOSE_HEADERS = [
    'Content-Range',   # ← browser needs this to know segment boundaries
    'Accept-Ranges',
]

# Application definition
SITE_ID = 1
INSTALLED_APPS = [
    'django_prometheus',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.postgres',  # required for pgvector HnswIndex system checks
    'django_filters',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework.authtoken',
    'rest_framework_simplejwt',
    # SECURITY: token_blacklist enables ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION
    # so a leaked refresh token can be invalidated (used by the /auth/logout/ endpoint).
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',#for frontend
    'storages',#S3-compatible MEDIA storage — see STORAGES["default"] below
    ##
    
    # Local Apps
    'backend.app',
    ##
    'django_redis',
    'django.contrib.sites',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    'dj_rest_auth',
    #'rest_framework_simplejwt',
    'dj_rest_auth.registration',
    'django_celery_beat',
    
    
]

MIDDLEWARE = [
    'django_prometheus.middleware.PrometheusBeforeMiddleware',
    'corsheaders.middleware.CorsMiddleware',##
    # CorrelationIdMiddleware is high in the stack so it runs before
    # SecurityMiddleware (which can short-circuit with SECURE_SSL_REDIRECT
    # in production) — every request, even 301s, gets a correlation id.
    'backend.EchoFlow.middleware.CorrelationIdMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'allauth.account.middleware.AccountMiddleware',##
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'django_prometheus.middleware.PrometheusAfterMiddleware',
]

ROOT_URLCONF = 'backend.EchoFlow.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'backend.EchoFlow.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    'default': dj_database_url.config(
            default=os.environ.get('DATABASE_URL', ''),
            conn_max_age=600,
            conn_health_checks=True,
        )
}
# SEC: per-session safety timeouts. Without these, a single slow
# query (e.g. update_global_metrics over a large AudioClip table) or
# a stuck transaction can hold a backend connection indefinitely.
# With PgBouncer's DEFAULT_POOL_SIZE=25 in front of web/celery, 25
# slow queries would exhaust the pool and the app would stop
# accepting new connections. Each timeout trades a worse error
# (QueryCanceled / lock timeout / fatal "terminating connection due
# to idle-in-transaction timeout") for a better failure mode: the
# connection is freed and the request returns a 5xx the caller can
# retry. See docs/backend-bug-fixs.md item A1 and
# docs/EXPLAIN/decisions/partial-issues-completion-plan.md §1 for
# trade-off analysis.
#
# DECISION: apply these GUCs via the psycopg2 `options='-c ...'`
# connection string. Two requirements to make this work behind
# pgbouncer transaction mode:
#   1. pgbouncer's `ignore_startup_parameters` (set in
#      docker-compose.yml as IGNORE_STARTUP_PARAMETERS=...) must
#      include each of these GUC names. Without that whitelist,
#      pgbouncer rejects the connection at startup with
#        FATAL: unsupported startup parameter in options: statement_timeout
#      and every web/celery container crashes before its first query.
#      pgbouncer does NOT apply these settings itself — it only
#      ignores the unknown name so the client request can pass
#      through to Postgres, which honors the `-c` prefix.
#   2. `connect_timeout` is a libpq *connection* parameter (passed
#      to PQconnectdb), NOT a GUC. It MUST be a top-level psycopg2
#      kwarg. Putting it inside the `-c ...` string raises
#        `unrecognized configuration parameter "connect_timeout"`
#      because the server tries to SET it like a GUC and fails.
#
# The `options`/`connect_timeout` kwargs are psycopg2-specific;
# SQLite (used in tests) does not accept them. Skip when the engine
# is not Postgres. `dj_database_url` may return an empty dict when
# DATABASE_URL is unset; check the engine key to be safe.
if DATABASES['default'].get('ENGINE', '').endswith('postgresql'):
    if 'OPTIONS' not in DATABASES['default']:
        DATABASES['default']['OPTIONS'] = {}
    DATABASES['default']['OPTIONS']['options'] = (
        '-c statement_timeout=30s '
        '-c idle_in_transaction_session_timeout=60s '
        '-c lock_timeout=10s'
    )
    DATABASES['default']['OPTIONS']['connect_timeout'] = 10

# DECISION: optional 'read' connection for routing pure reads to a
# PostgreSQL streaming replica. See backend/app/db_routers.py and
# docs/EXPLAIN/database/05-read-replica-design.md. The replica is not
# provisioned by docker-compose yet; when READ_DATABASE_URL is unset,
# the router falls back to 'default' and the existing single-DB
# behavior is preserved unchanged. When set, every read on the 'app'
# app that is NOT inside transaction.atomic() and NOT a SELECT FOR
# UPDATE goes to the replica.
if os.environ.get('READ_DATABASE_URL'):
    DATABASES['read'] = dj_database_url.config(
        default=os.environ.get('READ_DATABASE_URL', ''),
        conn_max_age=600,
        # SECURITY: the replica is intended to be read-only. We set the
        # connection option as a defense in depth — if a bug ever causes
        # a write to be routed to 'read' (e.g. router bug, or a
        # developer manually using 'read' from a Celery task), Postgres
        # will refuse the write with
        # `ERROR: cannot execute INSERT in a read-only transaction`.
        conn_health_checks=True,
    )
    # Same psycopg2 `options` parameter pattern as the default
    # connection. Pre-PR, the read block passed `options={...}` as
    # a kwarg to dj_database_url.config(), which the library does
    # not accept — settings would crash the moment READ_DATABASE_URL
    # was set. Fixing in this PR to unblock the A5 read-replica
    # activation plan. Only apply when the engine is Postgres.
    if DATABASES['read'].get('ENGINE', '').endswith('postgresql'):
        if 'OPTIONS' not in DATABASES['read']:
            DATABASES['read']['OPTIONS'] = {}
        DATABASES['read']['OPTIONS']['options'] = '-c default_transaction_read_only=on'

# DECISION: register ReadRouter only when the replica is configured.
# Enabling the router without READ_DATABASE_URL set would route reads
# to a non-existent connection and every read would fail with
# "connection does not exist" — a worse failure mode than today.
if 'read' in DATABASES:
    DATABASE_ROUTERS = ['backend.app.db_routers.ReadRouter']

REDIS_URL_DEFAULT = 'redis://localhost:6379/1'
REDIS_URL = os.getenv("REDIS_URL", REDIS_URL_DEFAULT)

# DECISION: Two Redis URLs in Docker (broker vs cache) so a feed-queue spike
# can't evict queued Celery tasks and vice versa. In Docker compose the broker
# runs with `--maxmemory-policy noeviction` (can't lose queued tasks) and the
# cache with `allkeys-lru` (feed queues evictable since refill is idempotent).
# Non-Docker dev collapses both to REDIS_URL — a single Redis on localhost is
# fine for one developer.
REDIS_BROKER_URL = os.getenv("REDIS_BROKER_URL", REDIS_URL)
REDIS_CACHE_URL = os.getenv("REDIS_CACHE_URL", REDIS_URL)

# This is how you connect Redis to Django
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_CACHE_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        }
   }
}
CELERY_TASK_ROUTES = {
    'backend.app.tasks.process_audio_to_hls': {'queue': 'heavy_media'},
    'backend.app.tasks.refill_user_feed': {'queue': 'fast_feed'},
}
# 3. CELERY CONFIGURATION
CELERY_BROKER_URL = REDIS_BROKER_URL
CELERY_RESULT_BACKEND = REDIS_BROKER_URL
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_WORKER_STATE_DB = None
CELERY_WORKER_POOL = 'prefork'
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_ACKNOWLEDGE_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True

# 4. MEDIA FILES (uploaded originals + generated HLS segments)
#
# DIAGNOSIS: every media bug this week (dead /media/ route under DEBUG=False,
# celery_media unable to see files web wrote, the wrong image entirely being
# served to a worker) traced back to one root assumption: that every
# container processing a clip shares one filesystem with the container that
# received the upload. That's only true in this specific docker-compose
# setup, and only true today because of a dev-convenience bind mount — it is
# NOT true of a real deployment (separate machines/nodes for web vs workers,
# autoscaled worker pools, no shared disk). Rather than keep patching volume
# paths to make the shared-filesystem assumption hold a little longer, we're
# removing the assumption: media now lives in S3-compatible object storage
# that every container reaches over the network, identically, in every
# environment. No shared volume, no bind-mount coincidence, no "works on my
# machine" — MEDIA_ROOT / FileSystemStorage no longer used for user content
# at all.
MEDIA_URL = '/media/'  # unused by S3Storage (which generates its own URLs);
                       # kept only because a few Django internals reference it
SCRAPER_SCRATCH_DIR = os.path.join(BASE_DIR, 'scratch')  # LOCAL, ephemeral,
    # per-container working space for downloading/decoding before upload to
    # object storage — never shared, never durable, never assumed to be
    # visible to any other container. tempfile-backed in code; this is the
    # equivalent of /tmp, just kept off the root filesystem for size reasons.

# Scraper defaults #############
SCRAPER_SOURCES = [
    'wikimedia', 'internet_archive', 'freesound', 'kaggle',
    'openverse', 'librivox', 'free_music_archive',
    # 'pixabay',          # DISABLED: needs SCRAPER_PIXABAY_API_KEY — uncomment once configured
    # 'podcast_index',    # DISABLED: needs SCRAPER_PODCAST_INDEX_API_KEY + _SECRET — uncomment once configured
    'podcast_rss', 'bbc_sound_effects',
    'musopen', 'loc_national_jukebox', 'usgov_audio',
]
SCRAPER_USER_AGENT = os.getenv('SCRAPER_USER_AGENT', 'EchoFlowScraper/1.0')
SCRAPER_CONTACT_EMAIL = os.getenv('SCRAPER_CONTACT_EMAIL', '')
SCRAPER_TARGET_DIR = os.path.join(SCRAPER_SCRATCH_DIR, 'audio_scraper')  # local
    # scratch space for raw downloads before scrapers upload to object
    # storage via the model's FileField .save(), same as everything else
SCRAPER_DEFAULT_CLIP_SECONDS = int(os.getenv('SCRAPER_DEFAULT_CLIP_SECONDS', '300'))
SCRAPER_MAX_DOWNLOADS_PER_MIN = int(os.getenv('SCRAPER_MAX_DOWNLOADS_PER_MIN', '30'))
SCRAPER_ALLOW_LICENSES = os.getenv('SCRAPER_ALLOW_LICENSES', 'CC0,CC-BY,CC-BY-SA,CC-BY-NC').split(',')
# DECISION: Resumable scraper state lives in SCRAPER_SCRATCH_DIR by default
# (a LOCAL directory on the web container — same convention as the
# downloader's tmp scratch space). Override via env for testing.
SCRAPER_STATE_DIR = os.getenv('SCRAPER_STATE_DIR', '') or None
SCRAPER_LOG_DIR = os.getenv('SCRAPER_LOG_DIR', '') or None
SCRAPER_DOWNLOAD_MAX_ATTEMPTS = int(os.getenv('SCRAPER_DOWNLOAD_MAX_ATTEMPTS', '3'))
SCRAPER_DOWNLOAD_BACKOFF = float(os.getenv('SCRAPER_DOWNLOAD_BACKOFF', '2.0'))
FREESOUND_API_KEY = os.getenv('FREESOUND_API_KEY', '')
SCRAPER_KAGGLE_LOCAL_PATH = os.getenv('SCRAPER_KAGGLE_LOCAL_PATH', '')

# DECISION: NC and SA gates default OFF. Operators opt-in by setting the env
# vars. NC content is included in the catalog but excluded from feed queries
# until SCRAPER_ALLOW_NC=True. CC-BY-SA content is imported with
# requires_share_alike=True + moderation_approved=False until an operator
# calls /clips/{id}/approve-moderation/ to opt-in each item.
SCRAPER_ALLOW_NC = os.getenv('SCRAPER_ALLOW_NC', 'False').lower() in ('1', 'true', 'yes')
SCRAPER_ALLOW_SHARE_ALIKE = os.getenv('SCRAPER_ALLOW_SHARE_ALIKE', 'False').lower() in ('1', 'true', 'yes')

# DECISION: Connector-specific API keys are namespaced with SCRAPER_* to keep
# the env surface consistent with existing SCRAPER_* settings. Sources that
# require a key return [] + WARNING when absent (freesound pattern).
SCRAPER_OPENVERSE_API_KEY = os.getenv('SCRAPER_OPENVERSE_API_KEY', '')
SCRAPER_PIXABAY_API_KEY = os.getenv('SCRAPER_PIXABAY_API_KEY', '')
SCRAPER_PODCAST_INDEX_API_KEY = os.getenv('SCRAPER_PODCAST_INDEX_API_KEY', '')
SCRAPER_PODCAST_INDEX_API_SECRET = os.getenv('SCRAPER_PODCAST_INDEX_API_SECRET', '')
SCRAPER_PODCAST_RSS_DEFAULT = os.getenv('SCRAPER_PODCAST_RSS_DEFAULT', '')

# DECISION: 300s default (5 min). EchoFlow is short-form audio.
# SCRAPER_DEFAULT_CLIP_SECONDS=300 is the equivalent for scraped imports;
# user uploads get the same cap for consistency. Group C item 23 fix.
MAX_DURATION_SECONDS = int(os.getenv('MAX_DURATION_SECONDS', '300'))

# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Celery Beat — periodic task schedule
CELERY_BEAT_SCHEDULE = {
    'update-global-metrics': {
        'task': 'backend.app.tasks.update_global_metrics',
        'schedule': 300.0,  # every 5 minutes
    },
    'evolve-user-baselines': {
        # DECISION: Hourly is too aggressive — with limit=100 per user and
        # select_related('clip'), this scans 100 interactions per user per
        # hour. At 100k users that's 10M interaction reads/hour. Daily (86400s)
        # is the design intent. Use crontab in django_celery_beat for 3:00 AM
        # if exact timing matters.
        'task': 'backend.app.tasks.evolve_long_term_user_baselines',
        'schedule': 86400.0,  # every 24 hours
    },
    'cleanup-stuck-processing': {
        # SECURITY/RELIABILITY: A clip stuck in 'processing' past 15 minutes
        # means its Celery task never completed (Redis broker hiccup, worker
        # OOM, network drop). Without this task the clip is abandoned.
        # Re-enqueue and let the retry decorators handle transient failures.
        'task': 'backend.app.tasks.cleanup_stuck_processing',
        'schedule': 300.0,  # every 5 minutes
    },
    'flush-telemetry-stream': {
        # Stream consumer. Primary path. XREADGROUP drains
        # stream:interaction.events every 10s with 5s BLOCK, dedups
        # via processed_event:{event_id} SETNX (24h TTL), bulk-inserts
        # UserInteraction rows, XACKs, and routes poison messages to
        # stream:interaction.events:dlq. Fast cadence is cheap with
        # consumer groups; replaces the per-request row-lock contention
        # that the architecture audit flags as the #1 scalability risk.
        'task': 'backend.app.tasks.flush_telemetry_stream',
        'schedule': 10.0,  # every 10 seconds
    },
    'flush-telemetry-legacy': {
        # Legacy LIST consumer. Kept for one operational cycle as a
        # safety net while the stream consumer proves itself. Drains
        # the 'telemetry:queue' Redis list (the producer's LIST fallback
        # when ECHOFLOW_TELEMETRY_STREAM=off or the stream is unhealthy).
        # TODO: remove after one cycle of stable operation.
        'task': 'backend.app.tasks.flush_telemetry_legacy',
        'schedule': 30.0,  # every 30 seconds
    },
    'rebuild-global-exploit-pool': {
        # P2.2: Global candidate pool. Single SELECT + ZADD rebuild
        # of the feed:exploit_pool ZSET. See
        # backend/app/services/feed_pool.py and
        # docs/EXPLAIN/recommendation/03-feed-pre-computation.md.
        'task': 'backend.app.tasks.rebuild_global_exploit_pool',
        'schedule': 300.0,  # every 5 minutes
    },
    'dispatch-user-pool-rebuilds': {
        # P2.2: Per-user explore pool fan-out. Runs hourly;
        # internally enqueues per-user rebuilds with a 0..3600s
        # countdown so the workers absorb them gradually.
        'task': 'backend.app.tasks.dispatch_user_pool_rebuilds',
        'schedule': 3600.0,  # every hour
    },
    'cleanup-orphan-hls': {
        # Group B item 12: defense-in-depth for post_delete signal
        # failures. Scans hls/ for prefixes whose clip_id is not
        # in AudioClip and deletes them. Bounded to 1000/run so
        # a runaway situation cannot page the operator. Daily at
        # 03:00 UTC (off-peak; uses crontab below instead of float
        # schedule to pin the hour).
        'task': 'backend.app.tasks.cleanup_orphan_hls',
        'schedule': crontab(minute=0, hour=3),
    },
    'flush-counters-to-pg': {
        # Group B item 9: drain the Redis counter store and apply
        # to Postgres (or just drain during Phase 1 dual-write).
        # Every 5 minutes; matches update_global_metrics cadence.
        # Phase 1 (default): the F() in UserInteraction.save() also
        # runs, so this task is a read-and-discard. Phase 2 (set
        # ECHOFLOW_DUAL_WRITE_COUNTERS=False in compose env): the
        # F() is bypassed and this task becomes the only path.
        'task': 'backend.app.tasks.flush_counters_to_pg',
        'schedule': 300.0,
    },
}
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'


# Feed candidate pool — pre-computation knobs
# (Group A item 7 in docs/unfixed-issues-2026-09-03.md)
# See docs/EXPLAIN/recommendation/03-feed-pre-computation.md for the
# full design (memory cost, staleness budget, fallback contract).
FEED_POOL_GLOBAL_TOP_N = int(os.environ.get('FEED_POOL_GLOBAL_TOP_N', '10000'))
FEED_POOL_USER_TOP_N = int(os.environ.get('FEED_POOL_USER_TOP_N', '1000'))
FEED_POOL_GLOBAL_TTL = int(os.environ.get('FEED_POOL_GLOBAL_TTL', '300'))
FEED_POOL_USER_TTL = int(os.environ.get('FEED_POOL_USER_TTL', '86400'))
FEED_POOL_REBUILD_CHUNK_SIZE = int(
    os.environ.get('FEED_POOL_REBUILD_CHUNK_SIZE', '1000')
)


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

# Enable WhiteNoise's compression and caching features.
# DECISION: STORAGES dict, not STATICFILES_STORAGE — that setting was removed
# in Django 5.1 and is silently ignored (no manifest would ever be generated).
STORAGES = {
    # Object storage, not the local disk — every service (web, every celery
    # worker, beat) reaches this over the network identically, so nothing
    # depends on which container wrote a file or which container reads it
    # back. Works against real S3, Cloudflare R2, or (for local dev) MinIO —
    # anything speaking the S3 API. See docker-compose.yml's `minio` service
    # for the local equivalent, and .env.example for the required vars.
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": os.environ["AWS_STORAGE_BUCKET_NAME"],
            "region_name": os.getenv("AWS_S3_REGION_NAME", "auto"),
            # None -> talks to real AWS S3. Set to MinIO/R2's endpoint for
            # anything else. This one var is the entire prod/dev difference —
            # there is no separate code path for "local mode".
            "endpoint_url": os.getenv("AWS_S3_ENDPOINT_URL") or None,
            "access_key": os.environ["AWS_ACCESS_KEY_ID"],
            "secret_key": os.environ["AWS_SECRET_ACCESS_KEY"],
            # Bucket is private. We hand out short-lived signed URLs instead
            # of a public bucket + permanent links, so a leaked/scraped URL
            # stops working after AWS_S3_QUERYSTRING_EXPIRE seconds.
            "default_acl": None,
            "querystring_auth": True,
            "querystring_expire": int(os.getenv("AWS_S3_QUERYSTRING_EXPIRE", "3600")),
            "file_overwrite": False,
            # path-style addressing (bucket.region.host/bucket/key style off)
            # is required for MinIO and most non-AWS S3-compatible endpoints;
            # real AWS accepts it too, so one setting covers both.
            "addressing_style": "path",
        },
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

# SECURITY / REGULATORY: Enforce India S3 region for DPDP / RBI compliance.
# DECISION: Runtime assertion rather than silent fallback; production must
# explicitly set ap-south-1 (or ap-south-2) or the app fails to start.
assert STORAGES["default"]["OPTIONS"]["region_name"] in ("ap-south-1", "ap-south-2", "auto"), 'STORAGES region must be set to ap-south-1, ap-south-2 (AWS S3 / DPDP / RBI) or "auto" (Cloudflare R2). Set AWS_S3_REGION_NAME in .env.'
# PUBLIC_MEDIA_ENDPOINT_URL: the endpoint a BROWSER can actually reach, as
# opposed to AWS_S3_ENDPOINT_URL above (which is what containers use to talk
# to the bucket over the Docker-internal network). These are frequently
# different hosts even in production — e.g. app servers reaching storage over
# a private/VPC endpoint while users need a public-facing URL — so this
# isn't a dev-only hack, it's the general shape of the problem.
#
# Locally: AWS_S3_ENDPOINT_URL=http://minio:9000 (container DNS name),
# PUBLIC_MEDIA_ENDPOINT_URL=http://localhost:9000 (host-published port) —
# same MinIO instance, reached two different ways depending on who's asking.
# In prod against real S3 these are typically identical (or you leave
# PUBLIC_MEDIA_ENDPOINT_URL unset and it falls back to AWS_S3_ENDPOINT_URL).
PUBLIC_MEDIA_ENDPOINT_URL = os.getenv("PUBLIC_MEDIA_ENDPOINT_URL") or os.getenv("AWS_S3_ENDPOINT_URL") or None

# HLS token protection settings (see docs/EXPLAIN/storage/04-hls-token-protection.md)
# MEDIA_TOKEN_SECRET: HMAC signing key shared between Django (token issuance)
#   and the Cloudflare Worker / nginx njs (token validation). Must be identical
#   or all HLS playback returns 403.
# MEDIA_TOKEN_TTL_SECONDS: token lifetime in seconds (default 600 = 10 min).
# MEDIA_TOKEN_COOKIE_DOMAIN: cookie Domain attribute. Leave empty for dev
#   (localhost does not support domain cookies). Set to parent domain (e.g.
#   ".echo-flow.in") in prod for cross-subdomain cookie sharing.
MEDIA_TOKEN_SECRET = os.getenv("MEDIA_TOKEN_SECRET", "")
MEDIA_TOKEN_TTL_SECONDS = int(os.getenv("MEDIA_TOKEN_TTL_SECONDS", "600"))
MEDIA_TOKEN_COOKIE_DOMAIN = os.getenv("MEDIA_TOKEN_COOKIE_DOMAIN", "")
AUTH_USER_MODEL = 'app.User' # for Custom user model
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
        'rest_framework.throttling.ScopedRateThrottle',
    ],
    # SECURITY: per-scope rates for abuse-prone endpoints. Each ViewSet
    # opts in with `throttle_scope = 'X'` to inherit its rate. These are
    # defaults — the architecture audit calls log_telemetry the #1 abuse
    # vector (viewbot / engagement-velocity manipulation), so its rate is
    # the tightest. Override via env vars if needed.
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/hour',
        'user': '1000/hour',
        'telemetry': '60/min',      # log_telemetry: 1/second max sustained
        'upload': '20/hour',        # AudioUploadViewSet.create: prevent storage abuse
        'register': '5/hour',       # RegisterView: prevent account-creation spam
        'login': '10/min',          # TokenObtainPairView: prevent credential stuffing
        'comment': '60/hour',       # CommentViewSet.create
        'share_send': '100/hour',   # ShareViewSet.send_share (anti-spam)
        'share_poll': '1000/hour',  # ShareViewSet inbox/unread/mark-read (client polling)
        'interaction': '60/min',    # toggle_like, register_skip
        'legal': '30/hour',       # ComplianceContactView / TakedownRequestView (issue-03/05)
        'grievance': '10/hour',   # GrievanceCreateView (issue-03)
        'data_subject': '5/hour', # DataSubjectAccessView / Erasure (issue-06)
    },
}
# lets set lifetimes for tokens
from datetime import timedelta

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=15),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    # SECURITY: Rotate refresh tokens on every /token/refresh/ call. The previous
    # refresh token is blacklisted (if 'token_blacklist' is in INSTALLED_APPS).
    # Tradeoff: clients must update their stored refresh token on every refresh,
    # but a stolen refresh token is single-use.
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
}

# Structured logging configuration
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {
        # Inject the per-request correlation_id (set by CorrelationIdMiddleware
        # via contextvars) into every log record. Empty string outside a
        # request scope (e.g., Celery workers) — see celery.py to set it
        # from task headers.
        'correlation': {
            '()': 'backend.EchoFlow.logging_filters.CorrelationIdFilter',
        },
    },
    'formatters': {
        'json': {
            '()': 'pythonjsonlogger.jsonlogger.JsonFormatter',
            'fmt': '%(asctime)s %(name)s %(levelname)s %(correlation_id)s user=%(user_id)s ip=%(client_ip)s endpoint=%(endpoint_path)s %(message)s',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'json',
            'filters': ['correlation'],
        },
    },
    'root': {
        'handlers': ['console'],
        'level': os.environ.get('LOG_LEVEL', 'INFO'),
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': os.environ.get('DJANGO_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
        'backend.app': {
            'handlers': ['console'],
            'level': os.environ.get('APP_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
        'celery': {
            'handlers': ['console'],
            'level': os.environ.get('CELERY_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
    },
}

# SECURITY: Production-only cookie + transport hardening.
# Wrapped in `if not DEBUG:` so the dev server (HTTP) keeps working.
# In any environment that terminates TLS (Traefik / nginx / CloudFront),
# SECURE_PROXY_SSL_HEADER is required or SECURE_SSL_REDIRECT will loop.
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    CSRF_COOKIE_SAMESITE = 'Lax'
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')


# DECISION: Regulatory settings (TERMS_VERSIONS, compliance/grievance/nodal contacts) live in settings.py rather than a DB table so they are env-driven and change without migration. Tradeoff: no audit trail of officer changes (operational, not regulatory requirement); DB table would require migration per change. See models.py Grievance/AuditLog for DB-level audit of grievances and identity.
TERMS_VERSIONS = os.environ.get('TERMS_VERSIONS', 'v1.0').split(',')
COMPLIANCE_OFFICER_NAME = os.environ.get('COMPLIANCE_OFFICER_NAME', 'EchoFlow Compliance Officer')
COMPLIANCE_OFFICER_EMAIL = os.environ.get('COMPLIANCE_OFFICER_EMAIL', 'compliance@echoflow.in')
GRIEVANCE_OFFICER_NAME = os.environ.get('GRIEVANCE_OFFICER_NAME', 'EchoFlow Grievance Officer')
GRIEVANCE_OFFICER_EMAIL = os.environ.get('GRIEVANCE_OFFICER_EMAIL', 'grievance@echoflow.in')
NODAL_CONTACT_NAME = os.environ.get('NODAL_CONTACT_NAME', 'EchoFlow Nodal Contact')
NODAL_CONTACT_EMAIL = os.environ.get('NODAL_CONTACT_EMAIL', 'nodal@echoflow.in')
VERSION = '1.0.0'