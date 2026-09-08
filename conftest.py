"""Pytest fixtures and configuration for the EchoFlow test suite.

ALL tests run against PostgreSQL inside the Docker `web` container.
No SQLite fallback, no stub migrations, no bare-metal test mode.

The test database is `echoflow_test` — created automatically on first
run by this conftest (using psycopg2 to connect to the Postgres instance
and CREATE DATABASE). It is dropped on session teardown.

Pgvector extension is installed on `template1` so every CREATE DATABASE
inherits it — no migration hackery needed.
"""
import os
import sys
from pathlib import Path

# Set required env vars BEFORE django.setup() — settings.py reads them.
os.environ.setdefault('DJANGO_SECRET_KEY', 'test-secret-key-not-for-prod')
os.environ.setdefault('DJANGO_DEBUG', 'True')
os.environ.setdefault('AWS_STORAGE_BUCKET_NAME', 'test-bucket')
os.environ.setdefault('AWS_ACCESS_KEY_ID', 'test')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'test')

# Override DATABASE_URL to point directly at the postgres container
# (bypassing pgbouncer). pgbouncer is configured to whitelist only
# `echoflow_db`, but the test DB is `echoflow_test`. Direct connection
# lets pytest create + use `echoflow_test` without pgbouncer blocking.
# We re-parse the existing URL and swap host/port/db.
import urllib.parse as _urlparse
_existing_url = os.environ.get('DATABASE_URL', '')
if _existing_url:
    _parsed = _urlparse.urlparse(_existing_url)
    _new_netloc = _parsed.netloc.replace('pgbouncer:6432', 'db:5432')
    _new_path = '/echoflow_test'
    _test_url = _urlparse.urlunparse(
        _parsed._replace(netloc=_new_netloc, path=_new_path))
    os.environ['DATABASE_URL'] = _test_url
    # Also set PG* env vars that some tooling reads directly (bypasses
    # Django's settings cache when subprocesses are spawned).
    os.environ['PGHOST'] = 'db'
    os.environ['PGPORT'] = '5432'
    os.environ['PGDATABASE'] = 'echoflow_test'
    print(f'[conftest] DATABASE_URL overridden to: {_test_url}', file=sys.stderr)

# Add the repo root to sys.path so 'backend.EchoFlow.settings' resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import django
from django.conf import settings

django.setup()

# Force test database name. Conftest auto-creates `echoflow_test` if it
# doesn't exist, so the real migrations run against a clean Postgres DB.
# This is the root-cause fix for the previous 178 `auth_group does not exist`
# errors: we no longer fight pytest-django's hook ordering with SQLite
# overrides. We just use Postgres with real migrations.
import sys as _sys
print(f'[conftest] BEFORE override: NAME={settings.DATABASES["default"].get("NAME")!r}', file=_sys.stderr)
if settings.DATABASES['default'].get('NAME') != 'echoflow_test':
    db = settings.DATABASES['default'].copy()
    db['NAME'] = 'echoflow_test'
    # Also set TEST['NAME'] so pytest-django doesn't re-prefix with 'test_'
    if 'TEST' not in db:
        db['TEST'] = {}
    db['TEST']['NAME'] = 'echoflow_test'
    # Bypass pgbouncer for tests: it whitelists only `echoflow_db`.
    db['HOST'] = 'db'
    db['PORT'] = 5432
    settings.DATABASES['default'] = db
print(f'[conftest] AFTER override: NAME={settings.DATABASES["default"].get("NAME")!r} HOST={settings.DATABASES["default"].get("HOST")!r}', file=sys.stderr)


import psycopg2
import pytest
import sys as _sys

print(f'[conftest] Final DATABASES: {settings.DATABASES["default"]!r}', file=_sys.stderr)
print(f'[conftest] env DATABASE_URL: {os.environ.get("DATABASE_URL")}', file=_sys.stderr)

# Patch dj_database_url.config so any later re-eval of settings.py gets
# the test-DB values too.
import dj_database_url as _dju
_orig_config = _dju.config


def _patched_config(*args, **kwargs):
    result = _orig_config(*args, **kwargs)
    if isinstance(result, dict) and 'HOST' in result:
        result['HOST'] = 'db'
        result['PORT'] = 5432
        result['NAME'] = 'echoflow_test'
    return result
_dju.config = _patched_config

# Patch the existing default DatabaseWrapper's settings_dict. pytest-django
# instantiates DatabaseWrapper from the original DATABASES dict at startup;
# our conftest override of settings.DATABASES doesn't reach into the
# wrapper's settings_dict. We patch it directly so the wrapper's
# get_connection_params() returns db:5432/echoflow_test.
from django.db import connection as _default_connection
_orig_settings_dict = _default_connection.settings_dict
_default_connection.settings_dict = {
    **_orig_settings_dict,
    'HOST': 'db',
    'PORT': 5432,
    'NAME': 'echoflow_test',
    'TEST': {**_orig_settings_dict.get('TEST', {}), 'NAME': 'echoflow_test'},
}
# Also reset the cached connection so the next ensure_connection uses
# the new settings.
_default_connection.connection = None
print(f'[conftest] Patched default connection settings_dict: '
      f'HOST={_default_connection.settings_dict.get("HOST")} '
      f'NAME={_default_connection.settings_dict.get("NAME")}', file=_sys.stderr)


def _install_pgvector_on_template1():
    """Install pgvector extension on template1 so every new DB inherits it.

    The real 0001_initial.py runs `CREATE EXTENSION IF NOT EXISTS vector;`
    which is Postgres-only. By installing it on template1, every CREATE
    DATABASE (including echoflow_test) is born with `vector` already loaded.
    """
    db = settings.DATABASES['default']
    if not db.get('ENGINE', '').endswith('postgresql'):
        return

    target_user = db.get('USER', '')
    target_password = db.get('PASSWORD', '')
    target_host = db.get('HOST', '') or 'localhost'
    target_port = db.get('PORT', '') or 5432

    # Connect to template1 as the test DB user to install pgvector.
    # If that user lacks superuser privileges, fall back to connecting
    # as the default postgres superuser (common in Docker setups).
    admin_user = target_user or 'postgres'
    admin_password = target_password or ''
    admin_host = target_host or 'localhost'
    admin_port = target_port or '5432'

    try:
        admin_conn = psycopg2.connect(
            host=admin_host,
            port=admin_port,
            user=admin_user,
            password=admin_password,
            dbname='template1',
        )
    except psycopg2.OperationalError:
        # Fallback: try connecting without password (trust auth)
        try:
            admin_conn = psycopg2.connect(
                host=admin_host,
                port=admin_port,
                user=admin_user,
                dbname='template1',
            )
        except psycopg2.OperationalError:
            # If we can't connect to template1, pgvector may already be
            # installed or the test user has superuser on the target DB.
            # Skip — tests will fail with a clear error if extension is missing.
            return

    admin_conn.autocommit = True
    try:
        with admin_conn.cursor() as cur:
            cur.execute('CREATE EXTENSION IF NOT EXISTS vector;')
    finally:
        admin_conn.close()


def _create_test_database():
    """Create `echoflow_test` if it doesn't already exist.

    Connects to the Postgres instance (defaulting to `postgres` DB) and
    runs CREATE DATABASE if the test DB is missing. This lets developers
    run tests without manually creating the DB first.
    """
    db = settings.DATABASES['default']
    test_name = db.get('NAME', 'echoflow_test')
    if not test_name:
        return

    if not db.get('ENGINE', '').endswith('postgresql'):
        return

    target_user = db.get('USER', 'postgres')
    target_password = db.get('PASSWORD', '')
    target_host = db.get('HOST', 'localhost') or 'localhost'
    target_port = db.get('PORT', 5432) or 5432

    # Connect to the default `postgres` DB to check/create the test DB.
    try:
        admin_conn = psycopg2.connect(
            host=target_host,
            port=target_port,
            user=target_user,
            password=target_password,
            dbname='postgres',
        )
    except psycopg2.OperationalError:
        # Fallback: try without password
        try:
            admin_conn = psycopg2.connect(
                host=target_host,
                port=target_port,
                user=target_user,
                dbname='postgres',
            )
        except psycopg2.OperationalError:
            # Can't connect — assume DB exists or will be created by CI.
            return

    admin_conn.autocommit = True
    try:
        with admin_conn.cursor() as cur:
            # Check if test DB exists
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s;",
                (test_name,),
            )
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{test_name}";')
    finally:
        admin_conn.close()


def _drop_test_database():
    """Drop `echoflow_test` on session teardown (optional cleanup).

    Only drops connections if the test DB exists. Skips silently if
    the DB doesn't exist or we can't connect.
    """
    db = settings.DATABASES['default']
    test_name = db.get('NAME', 'echoflow_test')
    if not test_name:
        return

    if not db.get('ENGINE', '').endswith('postgresql'):
        return

    target_user = db.get('USER', 'postgres')
    target_password = db.get('PASSWORD', '')
    target_host = db.get('HOST', 'localhost') or 'localhost'
    target_port = db.get('PORT', 5432) or 5432

    try:
        admin_conn = psycopg2.connect(
            host=target_host,
            port=target_port,
            user=target_user,
            password=target_password,
            dbname='postgres',
        )
    except psycopg2.OperationalError:
        return

    admin_conn.autocommit = True
    try:
        with admin_conn.cursor() as cur:
            # Terminate existing connections first
            cur.execute(
                """SELECT pg_terminate_backend(pid)
                   FROM pg_stat_activity
                   WHERE datname = %s AND pid <> pg_backend_pid();""",
                (test_name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{test_name}";')
    finally:
        admin_conn.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_sessionstart(session):
    """Install pgvector on template1 and create echoflow_test DB."""
    _install_pgvector_on_template1()
    _create_test_database()
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_sessionfinish(session, exitstatus):
    """Drop echoflow_test DB on session teardown.

    Set ECHOFLOW_KEEP_TEST_DB=1 to skip the drop (handy when iterating on
    schema changes — keeps the DB between runs).
    """
    yield
    if not os.environ.get('ECHOFLOW_KEEP_TEST_DB'):
        _drop_test_database()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def user(django_user_model):
    """A standard active user."""
    return django_user_model.objects.create_user(
        username='alice', email='alice@example.com', password='test-pass-1234'
    )


@pytest.fixture
def other_user(django_user_model):
    """A second user for social tests (follow, share, comment)."""
    return django_user_model.objects.create_user(
        username='bob', email='bob@example.com', password='test-pass-1234'
    )


@pytest.fixture
def api_client():
    """An unauthenticated DRF test client."""
    from rest_framework.test import APIClient
    return APIClient()


@pytest.fixture
def auth_client(api_client, user):
    """An authenticated DRF test client (logged in as `user`)."""
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def ready_clip(user):
    """An AudioClip in 'ready' state with valid vectors."""
    from backend.app.models import AudioClip
    return AudioClip.objects.create(
        title='Test Clip',
        category='comedy',
        creator=user,
        status='ready',
        duration_ms=60_000,
        likes=0, shares=0, skips=0, comment_count=0,
        semantic_vector=[0.1] * 384,
        acoustic_vector=[0.1] * 128,
    )


@pytest.fixture
def processing_clip(user):
    """An AudioClip in 'processing' state (for cleanup_stuck_processing tests).

    The cleanup_stuck_processing task uses created_at to detect clips
    older than `threshold_minutes`. We set created_at to 30 min ago so
    the task considers this clip stuck. (AudioClip has auto_now_add=True
    on created_at, so we must use .update() to bypass the auto-set.)
    """
    from backend.app.models import AudioClip
    from django.utils import timezone
    from datetime import timedelta
    old = timezone.now() - timedelta(minutes=30)
    clip = AudioClip.objects.create(
        title='Stuck Clip',
        category='comedy',
        creator=user,
        status='processing',
        duration_ms=60_000,
    )
    # Bypass auto_now_add by writing directly via .update().
    AudioClip.objects.filter(pk=clip.pk).update(created_at=old)
    clip.refresh_from_db()
    return clip
