import os
import shutil
import subprocess
import tempfile
import re
import threading
import uuid
import numpy as np
import logging
import librosa
from celery import shared_task
from django.db import OperationalError, transaction
from django.conf import settings
from django.core.files.storage import default_storage
from django.db import connection
from django.db.models import F, FloatField, ExpressionWrapper
from django.utils import timezone
from datetime import timedelta
from django.core.cache import cache
from pgvector.django import CosineDistance
from .models import AudioClip, UserInteraction, User
from .services.task_publisher import publish
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)

whisper_model = None
embedding_model = None
kw_model = None
_model_lock = threading.Lock()


def get_whisper_model():
    # DECISION: Use thread-safe double-checked locking instead of simple
    # singleton to prevent concurrent task initialization from loading the
    # same ML model multiple times (memory waste / duplicate GPU/CPU loads).
    # Tradeoff: Slight latency on first access vs. guaranteed single init.
    global whisper_model
    if whisper_model is None:
        with _model_lock:
            if whisper_model is None:
                from faster_whisper import WhisperModel
                try:
                    whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
                    logger.info("WhisperModel initialized successfully.")
                except Exception as e:
                    logger.exception("Failed to initialize WhisperModel: %s", e)
                    raise
    return whisper_model


def get_embedding_model():
    global embedding_model
    if embedding_model is None:
        with _model_lock:
            if embedding_model is None:
                from sentence_transformers import SentenceTransformer
                try:
                    embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
                    logger.info("Embedding model initialized successfully.")
                except Exception as e:
                    logger.exception("Failed to initialize embedding model: %s", e)
                    raise
    return embedding_model


def get_kw_model():
    global kw_model
    if kw_model is None:
        with _model_lock:
            if kw_model is None:
                from keybert import KeyBERT
                try:
                    kw_model = KeyBERT()
                    logger.info("KeyBERT initialized successfully.")
                except Exception as e:
                    logger.exception("Failed to initialize KeyBERT: %s", e)
                    raise
    return kw_model

def extract_acoustic_vector(y,sr):
    """
    Extracts exactly 128 acoustic features representing the "vibe" of the audio.
    
    ALGORITHM: Acoustic Feature Extraction for Audio "Vibe" Matching
    This function uses librosa to extract multi-dimensional audio characteristics:
    - MFCC (40 dims): Captures timbre and voice texture for speaker/instrument recognition
    - Chroma (12 dims): Captures harmonic content and musical pitch characteristics
    - Mel Spectrogram (76 dims): Captures energy distribution across frequency ranges
    
    These 128 dimensions create a normalized vector used for finding audio with similar acoustic properties.
    The normalization ensures consistent cosine similarity calculations across the platform.
    
    Args:
        file_path (str): Path to the audio file to process
        
    Returns:
        list: 128-dimensional normalized audio feature vector
    """
    # Load audio (downsample to 22050Hz for faster processing)
    #y, sr = librosa.load(file_path, sr=22050)
    
    # 1. Mel-frequency cepstral coefficients (Timbre/Voice texture) - 40 dims
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=40).mean(axis=1)
    
    # 2. Chroma feature (Harmonic/Musical pitch) - 12 dims
    chroma = librosa.feature.chroma_stft(y=y, sr=sr).mean(axis=1)
    
    # 3. Mel Spectrogram (Energy across frequencies) - 76 dims
    mel = librosa.feature.melspectrogram(y=y, sr=sr, n_mels=76).mean(axis=1)
    
    # Concatenate into exactly 128 dimensions
    acoustic_vector = np.concatenate((mfcc, chroma, mel))
    
    # Normalize the vector for Cosine Similarity math
    norm = np.linalg.norm(acoustic_vector)
    if norm > 0:
        acoustic_vector = acoustic_vector / norm
        
    return acoustic_vector.tolist()


def normalize_to_wav(input_file_path, sr=22050):
    """Decode arbitrary input audio (mp3/webm/ogg/m4a/whatever a client sends)
    into a clean mono PCM WAV via ffmpeg, up front.

    This exists because librosa.load() tries soundfile (libsndfile) first and
    silently falls back to the deprecated `audioread` path — logging a
    UserWarning/FutureWarning — on any container/codec libsndfile can't
    decode. Direct browser/file uploads hit this because, unlike the scraper
    ingestion path (which already runs everything through
    scrapers/normalizer.py), nothing normalizes user uploads before they're
    handed to librosa. Doing one authoritative ffmpeg decode here removes the
    audioread fallback entirely (so this doesn't silently start hard-failing
    when librosa 1.0 drops that fallback) and gives every downstream step
    (librosa, Whisper, ffmpeg HLS) the same known-good source file instead of
    each re-decoding the original independently.

    Raises subprocess.CalledProcessError if ffmpeg can't decode the input at
    all (i.e. the upload is genuinely not a valid audio file).
    """
    fd, wav_path = tempfile.mkstemp(suffix='.wav')
    os.close(fd)
    command = [
        'ffmpeg', '-y', '-i', input_file_path,
        '-ac', '1', '-ar', str(sr),
        '-f', 'wav', wav_path,
    ]
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return wav_path


RETRYABLE_ERRORS = (
    OperationalError,
    ConnectionError,
    subprocess.CalledProcessError,
    OSError,
)

# DECISION: Added bind=True + retry config to prevent permanent data loss
# when transient errors occur (DB connection timeout, FFmpeg crash, network
# failure). Tradeoff: Slightly more overhead per task (retry tracking) vs.
# guaranteed resilience.
@shared_task(bind=True, max_retries=3, default_retry_delay=60, autoretry_for=RETRYABLE_ERRORS, retry_backoff=True, retry_backoff_max=600, retry_jitter=False)
def process_audio_to_hls(self, clip_id):

    # DECISION: Time the entire task with the hls_processing histogram.
    # Outcome is observed at the end: 'success' (normal return), 'terminal_error'
    # (caught and clip marked failed — won't retry), or 'error' (uncaught
    # exception, Celery will retry per the decorator).
    from . import metrics
    with metrics.time_hls_processing() as timer:
        return _process_audio_to_hls_impl(self, clip_id, timer)


def _process_audio_to_hls_impl(self, clip_id, timer):
    # Now 'clip' is guaranteed to exist for the following logic
    logger.info("process_audio_to_hls Task is starting...")
    clip = AudioClip.objects.get(id=clip_id)

    # ISSUE-04: Block HLS generation if moderation has not passed.
    # DECISION: Option A (v1): moderation_approved boolean. The task
    # skips processing and logs a warning if False. The approve-moderation
    # endpoint must set it to True before HLS can run.
    # HACK: We don't fail the task (terminal_error) because moderation
    # is an operational gate, not a file error. A future version may
    # retry after approval; for v1 we just skip.
    if not clip.moderation_approved:
        logger.info("process_audio_to_hls: moderation not approved for clip %s; skipping HLS generation.", clip_id)
        timer.set_outcome('skipped')
        # Don't change status — leave as 'processing' so the operator
        # knows it hasn't been processed yet. The approve endpoint
        # will trigger processing separately.
        return

    if not clip.original_file:
        # Handle missing file error
        logger.error("Audio file for clip %s not found.", clip_id)
        timer.set_outcome('terminal_error')
        clip.status = 'failed'
        clip.save()
        return

    # DIAGNOSIS -> SOLUTION: `clip.original_file.path` only exists for
    # FileSystemStorage; S3Storage (see settings.STORAGES) has no local path
    # at all — the bytes live in the bucket, not on this container's disk.
    # ffmpeg/librosa/Whisper all need a real local file to operate on, so we
    # explicitly stream the object down to a local temp file once, work on
    # that, and delete it when done. This is the same shape as
    # normalize_to_wav() below on purpose: "pull remote bytes to a local
    # scratch file, process, clean up" is now the *only* pattern used
    # anywhere in this task — no code path assumes a shared filesystem.
    fd, input_file_path = tempfile.mkstemp(suffix=os.path.splitext(clip.original_file.name)[1] or '.bin')
    with os.fdopen(fd, 'wb') as local_copy:
        with clip.original_file.open('rb') as remote_file:
            shutil.copyfileobj(remote_file, local_copy)

    # Normalize once, up front, before anything tries to decode the raw
    # upload. See normalize_to_wav() docstring for why this exists.
    try:
        normalized_path = normalize_to_wav(input_file_path)
    except subprocess.CalledProcessError as e:
        logger.error("Failed to normalize audio for clip %s: %s", clip_id, e.stderr.decode())
        timer.set_outcome('terminal_error')
        clip.status = 'failed'
        clip.save()
        os.remove(input_file_path)
        return
    finally:
        # input_file_path's only job was feeding ffmpeg above; normalized_path
        # is what every later step reads from.
        if os.path.exists(input_file_path):
            os.remove(input_file_path)

    # Local scratch dir for the HLS segments ffmpeg is about to generate.
    # ffmpeg needs a real filesystem to write into — it can't target S3
    # directly — so we render locally, then upload every resulting file to
    # object storage, then remove the local copy. Nothing under this
    # directory is ever read by another container; it exists only for the
    # lifetime of this task on this worker.
    local_hls_dir = tempfile.mkdtemp(prefix=f'hls-{clip_id}-')

    try:
        # 1. Acoustic Vector Extraction.
        # N12 fix: distinguish transient vs terminal.
        # - librosa.load() OSError on a local file IS transient (disk
        #   hiccup, NFS blip, temp race). Re-raise so autoretry_for
        #   picks it up.
        # - Any other Exception (corrupt audio, unsupported codec) is
        #   terminal — mark failed, don't retry.
        try:
            y, sr = librosa.load(normalized_path, sr=22050)
        except OSError:
            logger.exception("librosa.load() transient error for clip %s; re-raising for retry", clip_id)
            raise
        except Exception as e:
            logger.exception("librosa.load() terminal error for clip %s: %s", clip_id, e)
            timer.set_outcome('terminal_error')
            clip.status = 'failed'
            clip.save()
            return
        clip.acoustic_vector = extract_acoustic_vector(y, sr)

        # CRITICAL FIX: Extract exact duration for completion_rate math
        clip.duration_ms = int(librosa.get_duration(y=y, sr=sr) * 1000)

        clip.save(update_fields=['acoustic_vector', 'duration_ms'])
        logger.info(f"Extracted acoustic vector and duration for clip {clip_id}")
        # 2. AUDIO TO TEXT (Whisper)
        # Same N12 pattern: OSError/ConnectionError (transient model-load
        # failure) re-raise; other exceptions (model logic error) are
        # terminal.
        try:
            # Lazy-init models to avoid startup cost during management commands
            model = get_whisper_model()
            segments, info = model.transcribe(normalized_path, beam_size=5)
            transcript_text = " ".join([segment.text for segment in segments]).strip()

            # B. Semantic Vector via sentence-transformers
            if transcript_text:
                embed_model = get_embedding_model()
                vector = embed_model.encode(transcript_text)
                clip.semantic_vector = vector.tolist()
                # Extracts top 3 unigrams (single words)
                keywords = get_kw_model().extract_keywords(
                    transcript_text,
                    keyphrase_ngram_range=(1, 1),
                    stop_words='english',
                    top_n=3,
                )
                logger.info(f"Extracted keywords for clip {clip_id}: {keywords}")
                clip.tags = [kw[0] for kw in keywords]
            else:
                # Fallback for purely instrumental tracks with no vocals
                clip.semantic_vector = [0.0] * 384
                clip.tags = ["instrumental"]

            # ISSUE-04: Run moderation checks on transcript (if exists) and tags.
            # For v1, we compare transcript_text and tags against blocked phrases.
            # If moderation fails, mark as rejected and stop HLS processing.
            from ..services import content_moderation as moderation_svc
            # The transcript_text variable is available in this scope.
            transcript_approved, transcript_reason = moderation_svc.check_transcript_for_prohibited_content(transcript_text if 'transcript_text' in locals() else None)
            tags_approved, tags_reason = moderation_svc.check_tags_for_prohibited_content(clip.tags)
            if not transcript_approved:
                logger.error("Moderation rejected clip %s (transcript): %s", clip_id, transcript_reason)
                clip.moderation_approved = False
                clip.status = 'rejected'
                clip.save(update_fields=['moderation_approved', 'status', 'tags'])
                timer.set_outcome('moderation_rejected')
                return
            if not tags_approved:
                logger.error("Moderation rejected clip %s (tags): %s", clip_id, tags_reason)
                clip.moderation_approved = False
                clip.status = 'rejected'
                clip.save(update_fields=['moderation_approved', 'status', 'tags'])
                timer.set_outcome('moderation_rejected')
                return

            # All moderation checks passed — set approved.
            clip.moderation_approved = True
        except (OSError, ConnectionError):
            logger.exception("AI inference transient error for clip %s; re-raising for retry", clip_id)
            raise
        except Exception as e:
            logger.exception("Local AI Processing Failed: %s", e)
            timer.set_outcome('terminal_error')
            clip.status = 'failed'
            clip.save()
            return

        # Encode HLS from the same normalized WAV, not the raw upload — one
        # authoritative decode instead of ffmpeg re-decoding the original
        # container a second time. Written to LOCAL scratch space, then
        # uploaded to object storage below.
        # DECISION: Use standard MPEG-TS segments explicitly.
        # Chrome's MSE decoder rejects fMP4 segments with certain AAC codec
        # configurations, producing "DecoderStatus::kUnsupportedConfig".
        # MPEG-TS is universally supported by hls.js and all browsers.
        # Newer FFmpeg defaults to fMP4, so we must explicitly set mpegts.
        command = [
            'ffmpeg', '-y', '-i', normalized_path,
            '-c:a', 'aac', '-ar', '44100', '-ac', '2', '-b:a', '128k',
            '-f', 'hls', '-hls_time', '4', '-hls_playlist_type', 'vod',
            '-hls_segment_type', 'mpegts',
            '-master_pl_name', 'master.m3u8',
            os.path.join(local_hls_dir, 'index.m3u8')
        ]

        # N12 fix: split HLS encode from S3 upload. ffmpeg CalledProcessError
        # is terminal (corrupt audio, unsupported codec — retrying won't help).
        # default_storage.save() failures are typically transient (S3 hiccup,
        # network blip) — re-raise so autoretry_for picks it up.
        try:
            subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            logger.error("FFmpeg HLS encode error for clip %s: %s", clip_id, e.stderr.decode())
            timer.set_outcome('terminal_error')
            clip.status = 'failed'
            clip.save()
            return

        # Upload to object storage. OSError / ConnectionError are
        # transient (S3 blip, network blip) — re-raise.
        storage_prefix = f"hls/{clip.id}"
        try:
            for root, _dirs, files in os.walk(local_hls_dir):
                for fname in files:
                    local_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(local_path, local_hls_dir)
                    storage_key = f"{storage_prefix}/{rel_path}".replace(os.sep, '/')
                    with open(local_path, 'rb') as fh:
                        default_storage.save(storage_key, fh)
        except (OSError, ConnectionError):
            logger.exception("S3 upload transient error for clip %s; re-raising for retry", clip_id)
            raise

        # DECISION: store the relative object KEY, not a full URL. A
        # signed S3 URL expires (see AWS_S3_QUERYSTRING_EXPIRE) — baking
        # one into the database would mean playback silently breaks an
        # hour after processing regardless of whether the clip is still
        # valid. The serializer generates a fresh signed URL from this
        # key on every read instead (see FeedClipSerializer).
        clip.hls_playlist_url = f"{storage_prefix}/master.m3u8"
        clip.status = 'ready'
        clip.save()
    finally:
        # Always clean up both local scratch areas, success or failure.
        try:
            os.remove(normalized_path)
        except OSError:
            pass
        shutil.rmtree(local_hls_dir, ignore_errors=True)

    # DECISION: The OpenAI transcription/embedding/tagging path was a
    # prototype ("for when i will have money for API"). The local Whisper +
    # SentenceTransformer + KeyBERT pipeline (lines ~200-326) is the
    # production path. The OpenAI block was a triple-quoted string statement,
    # not a comment, so it was never executed — but its presence confused
    # grep and review. Removed.


# ---------------------------------------------------------------------------
# Feed recommendation engine — re-export shim.
#
# DECISION: The actual task bodies and the pure-Python ranking logic
# live in `ai_ml.pipelines.feed_tasks` and `ai_ml.pipelines.recommendation`.
# This module re-exports the same names so that pre-migration call sites
# (`from backend.app.tasks import refill_user_feed`,
#  `from backend.app.tasks import calculate_time_decayed_vectors`)
# continue to work without source-level edits. The task names are
# pinned in the new module with `name='backend.app.tasks.<func>'` so
# `CELERY_TASK_ROUTES` and `CELERY_BEAT_SCHEDULE` resolve unchanged.
#
# See ai_ml/README.md "Future Migration" for the full rationale.
# Imported eagerly here (not lazily) so a circular import
# (ai_ml.pipelines.recommendation -> backend.app.models) is
# resolved at Django app-loading time, not at first task dispatch.
# ---------------------------------------------------------------------------
from ai_ml.pipelines.recommendation import calculate_time_decayed_vectors  # noqa: E402,F401
from ai_ml.pipelines.feed_tasks import (  # noqa: E402,F401
    refill_user_feed,
    rebuild_global_exploit_pool,
    dispatch_user_pool_rebuilds,
    rebuild_user_explore_pool,
)


# DEPRECATED — replaced by the event-driven flush_counters_to_pg.
#
# Historical context: this task performed the O(N) per-beat scan
# flagged in the architecture audit (`docs/backend-bug-fixs.md` and
# the audit's group C / engagement-metrics concern). It executed a
# correlated subquery on `userinteraction` for every ready
# AudioClip row to compute avg_completion_rate, plus a per-row
# engagement_velocity recomputation. The scan scaled linearly with
# the total clip count and was the dominant contributor to the
# 30s statement_timeout failures under viral load.
#
# All three responsibilities now live in flush_counters_to_pg:
#   * counter deltas:         _apply_counter_deltas
#   * avg_completion_rate:    _apply_completion_deltas
#   * engagement_velocity:    _apply_engagement_velocity
#
# The task is kept as a no-op stub for one operational cycle so we
# can detect any drift between the old and new pipelines (a Celery
# beat entry that fires a no-op is observable in metrics; a missing
# task name would be a deployment error). Safe to remove in the
# next release once the new pipeline is proven in production.
@shared_task(name='backend.app.tasks.update_global_metrics')
def update_global_metrics(self):
    """DEPRECATED. Replaced by flush_counters_to_pg (event-driven)."""
    logger.info(
        "update_global_metrics: deprecated no-op; flush_counters_to_pg "
        "is now authoritative for likes/shares/skips, "
        "avg_completion_rate, and engagement_velocity."
    )
    return "deprecated"


# Added retry config, batch_size=100 to prevent memory exhaustion with large user counts.
@shared_task(bind=True, max_retries=3, default_retry_delay=60, autoretry_for=RETRYABLE_ERRORS, retry_backoff=True, retry_backoff_max=600)
def evolve_long_term_user_baselines(self):
    """
    Recompute long-term semantic + acoustic vectors for every active user
    from the last `limit` interactions. Batched in groups of BATCH_SIZE
    to bound memory: the previous code accumulated ALL users in
    `users_to_update` before a single bulk_update, which is O(N) memory.

    Tradeoff: more database round-trips, but bounded memory. With
    BATCH_SIZE=100 and 100k users, peak memory is ~5 MB instead of ~500 MB.
    """
    BATCH_SIZE = 100
    users_batch = []
    total_updated = 0
    for user in User.objects.filter(is_active=True).iterator(chunk_size=BATCH_SIZE):
        new_sem, new_ac = calculate_time_decayed_vectors(user, limit=100)
        if new_sem is not None:
            user.long_term_semantic = new_sem
            user.long_term_acoustic = new_ac
        users_batch.append(user)
        if len(users_batch) >= BATCH_SIZE:
            User.objects.bulk_update(
                users_batch,
                ['long_term_semantic', 'long_term_acoustic'],
                batch_size=BATCH_SIZE,
            )
            total_updated += len(users_batch)
            users_batch = []
    if users_batch:
        User.objects.bulk_update(
            users_batch,
            ['long_term_semantic', 'long_term_acoustic'],
            batch_size=BATCH_SIZE,
        )
        total_updated += len(users_batch)
    return f"Evolved long-term vectors for {total_updated} users."


@shared_task(bind=True, max_retries=3, default_retry_delay=10, retry_backoff=True)
def flush_telemetry_legacy(self, max_events=1000):
    """LEGACY: drain the Redis 'telemetry:queue' list and bulk-insert.

    Kept for one operational cycle as a safety net while the new
    flush_telemetry_stream consumer proves itself. With ECHOFLOW_TELEMETRY_STREAM
    on, the producer writes to the stream and the list is empty. With the
    stream consumer offline, the producer falls back to the list and this
    task drains it.

    TODO: remove after one cycle of stable operation.
    """
    import json
    redis_client = cache.client.get_client()
    queue_key = 'telemetry:queue'
    events = []
    # LPOP in a tight loop. If queue is empty, returns None and we stop.
    for _ in range(max_events):
        raw = redis_client.lpop(queue_key)
        if raw is None:
            break
        try:
            events.append(json.loads(raw))
        except json.JSONDecodeError:
            logger.warning("flush_telemetry_legacy: dropped malformed event: %r", raw)
            continue

    if not events:
        return "No events to flush."

    # N5 fix: batch the FK lookups with in_bulk instead of per-event .get().
    # Old: 2 queries per event = 2000 queries for max_events=1000.
    # New: 2 queries total (one per FK table) regardless of event count.
    user_ids = {e['user_id'] for e in events}
    clip_ids = {e['clip_id'] for e in events}
    try:
        users_by_id = User.objects.in_bulk(user_ids)
        clips_by_id = AudioClip.objects.in_bulk(clip_ids)
    except Exception as exc:
        logger.error("flush_telemetry_legacy: in_bulk failed (%s); dropping batch", exc)
        return f"FK lookup failed: {exc}"

    # Materialize to ORM objects in one bulk_create.
    interactions = []
    for e in events:
        user = users_by_id.get(e['user_id'])
        clip = clips_by_id.get(e['clip_id'])
        if user is None or clip is None:
            # FK was deleted between XADD and now. Skip; ACKed-by-design
            # (event is in the legacy queue, single attempt).
            continue
        interactions.append(UserInteraction(
            user=user,
            clip=clip,
            interaction_type=e['action_type'],
            watch_time_ms=e['watch_time_ms'],
            completion_rate=e['completion_rate'],
            is_active=True,
        ))

    if not interactions:
        return "No valid events to flush."

    UserInteraction.objects.bulk_create(interactions, batch_size=500)
    return f"Flushed {len(interactions)} telemetry events to UserInteraction."


@shared_task(bind=True, max_retries=3, default_retry_delay=10, retry_backoff=True)
def flush_telemetry_stream(self, max_events=500, block_ms=5000):
    """Stream consumer: drain stream:interaction.events via XREADGROUP.

    Consumer group: cg:telemetry-flush. Each consumer reads up to
    max_events, dedups via processed_event:{event_id} SETNX, bulk-inserts
    new events, and XACKs them. Poison messages (malformed payload,
    downstream exception) are XADD'd to stream:interaction.events:dlq
    and XACK'd from the main stream so the pipeline never stalls.

    Triggered every 10s via Celery beat — faster cadence than the legacy
    task because streams + consumer groups have lower per-tick overhead
    than LPOP loops.

    Idempotency: the dedup key has a 24h TTL. A consumer that crashes
    after bulk_create but before XACK will cause the same event_id to
    be re-read on the next tick; SETNX returns False, the event is
    silently dropped, and the stream entry is XACK'd anyway. The DB
    already has the row from the prior run, so this is correct.
    """
    import json
    import time
    import redis as redis_lib
    from .services.interactions import STREAM_KEY, CONSUMER_GROUP

    # SECURITY: use a direct redis-py client to avoid stale-connection
    # timeout on xreadgroup (parent-process fork artifact). django_redis's
    # connection pool survives forks and may return a broken socket.
    # A fresh client guarantees a new TCP connection.
    from django.conf import settings as _s
    redis_url = _s.CACHES['default']['LOCATION']
    client = redis_lib.from_url(redis_url, socket_keepalive=True)
    try:
        # Ensure the consumer group exists. MKSTREAM creates the stream on
        # first call; the try/except swallows the BUSYGROUP error on
        # subsequent boots. Cheap idempotent setup.
        try:
            client.xgroup_create(STREAM_KEY, CONSUMER_GROUP, id='0', mkstream=True)
        except Exception:
            pass  # BUSYGROUP — already exists.

        consumer_name = f"celery-{os.getpid()}"
        # Non-blocking xreadgroup with retry loop. The blocking 'block'
        # parameter in xreadgroup raises "Timeout reading from socket" in
        # redis-py 8.x under Celery prefork (signal-interrupts the syscall).
        # A non-blocking read + sleep avoids the syscall entirely.
        response = None
        deadline = time.monotonic() + (block_ms / 1000.0)
        while response is None and time.monotonic() < deadline:
            try:
                response = client.xreadgroup(
                    CONSUMER_GROUP,
                    consumer_name,
                    {STREAM_KEY: '>'},
                    count=max_events,
                )
            except redis_lib.TimeoutError:
                # Socket-level timeout — retry with a short sleep to avoid
                # tight-loop spinning on a flaky connection.
                time.sleep(0.1)
            except Exception as exc:
                logger.warning("flush_telemetry_stream: xreadgroup failed: %s", exc)
                return f"xreadgroup failed: {exc}"

        if not response:
            return "No events to flush."

        # response shape: [(stream_name, [(entry_id, {fields}), ...])]
        entries: list[tuple[str, dict]] = []
        for _stream, items in response:
            for entry_id, fields in items:
                entries.append((entry_id, fields))

        dedup_ttl = 86400
        processed_ids: list[str] = []
        dlq_ids: list[str] = []
        # N5 fix: collect distinct FK ids FIRST, then resolve via in_bulk
        # once. Old code did User.objects.get() and AudioClip.objects.get()
        # per entry — 2 queries per event. New: 2 queries total.
        pending_entries: list[tuple[str, dict, str, str]] = []
        # pending_entries holds (entry_id, fields, user_id_str, clip_id_str) for
        # entries that passed dedup. We accumulate the FK ids, batch-resolve,
        # then materialize interactions in a second pass.

        for entry_id, fields in entries:
            try:
                event_id = fields.get('event_id') or entry_id
                payload_raw = fields.get('payload')
                if not payload_raw:
                    logger.warning("flush_telemetry_stream: empty payload on %s", entry_id)
                    dlq_ids.append(entry_id)
                    continue
                event = json.loads(payload_raw)
                user_id = event['user_id']
                clip_id = event['clip_id']
            except (KeyError, json.JSONDecodeError, TypeError) as exc:
                logger.warning(
                    "flush_telemetry_stream: malformed event %s (%s); routing to DLQ",
                    entry_id, exc,
                )
                dlq_ids.append(entry_id)
                continue

            # SETNX dedup. If another consumer (or a previous run of this
            # consumer after a crash) already processed this event, skip it
            # and ACK — the row is already in the DB.
            dedup_key = f"processed_event:{event_id}"
            try:
                first_time = client.set(dedup_key, '1', nx=True, ex=dedup_ttl)
            except Exception as exc:
                logger.warning("flush_telemetry_stream: dedup SET failed (%s); processing anyway", exc)
                first_time = True

            if not first_time:
                processed_ids.append(entry_id)
                continue

            pending_entries.append((entry_id, event, user_id, clip_id))

        # Batch-resolve FKs once for all entries that survived dedup.
        # DECISION: the stream payload carries str IDs (Redis Stream field
        # values are always bytes/strings). The DB models use BigAutoField
        # for User.id and UUIDField for AudioClip.id. Pre-PR, the code
        # passed the str IDs directly to .get() against the int-keyed
        # User.objects.in_bulk dict, which silently mismatched and fired
        # the "missing user/clip" warning path even when the data was
        # present. We cast to the correct type per field here. The
        # AudioClip.id cast goes through UUID() to handle the
        # BigAutoField vs UUIDField type difference.
        interactions: list[UserInteraction] = []
        if pending_entries:
            user_ids: set[int] = set()
            clip_ids: set = set()
            for _entry_id, _event, user_id, clip_id in pending_entries:
                try:
                    user_ids.add(int(user_id))
                except (TypeError, ValueError):
                    pass
                try:
                    import uuid as _uuid
                    clip_ids.add(_uuid.UUID(str(clip_id)))
                except (TypeError, ValueError, AttributeError):
                    pass
            try:
                users_by_id = User.objects.in_bulk(user_ids) if user_ids else {}
                clips_by_id = AudioClip.objects.in_bulk(clip_ids) if clip_ids else {}
            except Exception as exc:
                logger.error("flush_telemetry_stream: in_bulk failed (%s); routing all to DLQ", exc)
                for entry_id, _event, _u, _c in pending_entries:
                    dlq_ids.append(entry_id)
            else:
                import uuid as _uuid
                for entry_id, event, user_id, clip_id in pending_entries:
                    try:
                        user = users_by_id.get(int(user_id))
                    except (TypeError, ValueError):
                        user = None
                    try:
                        clip = clips_by_id.get(_uuid.UUID(str(clip_id)))
                    except (TypeError, ValueError, AttributeError):
                        clip = None
                    if user is None or clip is None:
                        logger.warning(
                            "flush_telemetry_stream: missing user/clip for %s; ACKing (data will be lost)",
                            entry_id,
                        )
                        processed_ids.append(entry_id)
                        continue
                    interactions.append(UserInteraction(
                        user=user,
                        clip=clip,
                        interaction_type=event['action_type'],
                        watch_time_ms=event['watch_time_ms'],
                        completion_rate=event['completion_rate'],
                        is_active=True,
                    ))
                    processed_ids.append(entry_id)

        if interactions:
            try:
                UserInteraction.objects.bulk_create(interactions, batch_size=500)
            except Exception as exc:
                logger.error("flush_telemetry_stream: bulk_create failed (%s); routing all to DLQ", exc)
                for entry_id, _ in entries:
                    if entry_id not in processed_ids:
                        dlq_ids.append(entry_id)
                    else:
                        processed_ids.remove(entry_id)
            else:
                # A3 cache invalidation: bulk_create succeeded, so each
                # affected user's user_vectors cache is now stale. Invalidate
                # each unique user once. One DEL per user is O(1) on Redis;
                # the alternative (one DEL per event) would be N calls for
                # N events from the same user, which is wasteful. Tradeoff:
                # a failure here only means the cache stays stale for up to
                # 15 min (the TTL), which is the same behavior as before this
                # wiring — never worse.
                from .services.interactions import invalidate_user_vectors_cache
                unique_user_ids = {i.user_id for i in interactions}
                for uid in unique_user_ids:
                    try:
                        invalidate_user_vectors_cache(uid)
                    except Exception as exc:
                        logger.warning(
                            "flush_telemetry_stream: cache invalidation failed for user %s (%s)",
                            uid, exc,
                        )

        # ACK everything we handled successfully.
        if processed_ids:
            try:
                client.xack(STREAM_KEY, CONSUMER_GROUP, *processed_ids)
            except Exception as exc:
                logger.warning("flush_telemetry_stream: xack failed for %d ids: %s",
                               len(processed_ids), exc)

        # Move poison messages to DLQ so the main stream advances. Keep them
        # observable (no AUTO-trim) so operators can XLEN the DLQ and triage.
        for entry_id in dlq_ids:
            try:
                client.xadd('stream:interaction.events:dlq', {
                    'original_id': entry_id,
                    'reason': 'malformed_or_duplicate',
                })
                client.xack(STREAM_KEY, CONSUMER_GROUP, entry_id)
            except Exception as exc:
                logger.error("flush_telemetry_stream: DLQ xadd failed for %s: %s", entry_id, exc)

        return (
            f"Flushed {len(interactions)} telemetry events; "
            f"DLQ-routed {len(dlq_ids)}; ACKed {len(processed_ids)}."
        )
    finally:
        client.close()


@shared_task
def cleanup_stuck_processing(threshold_minutes=15, max_per_run=50):
    """Re-enqueue clips stuck in 'processing' status past the threshold.

    Audit item 6.7: A Celery broker outage at the moment of
    transaction.on_commit(lambda: process_audio_to_hls.delay(clip.id))
    (views.py:101) silently leaves the clip in 'processing' forever.
    This task runs every 5 min via Celery beat and re-enqueues up to
    max_per_run stuck clips, with the same retry config to handle
    transient failures. After max_retries the clip is flipped to
    'failed' so it shows up in error reports.
    """
    threshold = timezone.now() - timedelta(minutes=threshold_minutes)
    stuck = (
        AudioClip.objects
        .filter(status='processing', created_at__lt=threshold)
        .order_by('created_at')[:max_per_run]
    )
    re_enqueued = 0
    give_up = 0
    give_up_threshold = timedelta(minutes=threshold_minutes * 3)
    for clip in stuck:
        # Cap retries: if the clip has been stuck for more than
        # `threshold_minutes * 3` (e.g., 45 min if threshold=15), it has
        # been re-enqueued at least 3 times and is still failing. Mark
        # it as 'failed' so it shows up in error reports and stops
        # re-entering the queue.
        age = timezone.now() - clip.created_at
        if age > give_up_threshold:
            clip.status = 'failed'
            clip.save(update_fields=['status'])
            give_up += 1
            continue
        publish(process_audio_to_hls, str(clip.id))
        re_enqueued += 1
    if give_up:
        return f"Re-enqueued {re_enqueued}, gave up on {give_up} (>{int(give_up_threshold.total_seconds() // 60)}m) clips."
    return f"Re-enqueued {re_enqueued} stuck clips (threshold={threshold_minutes}m)."


@shared_task(bind=True, max_retries=3, default_retry_delay=60, autoretry_for=RETRYABLE_ERRORS, retry_backoff=True, retry_backoff_max=600)
def scrape_and_import(self, source_name, limit=5, clip_length=300, allow_nc=None, include_share_alike=None):
    """Celery task wrapper to run a scraper source and import clips.

    This task delegates to the source connectors and uses the local
    downloader/normalizer/uploader to create `AudioClip` records and
    then triggers `process_audio_to_hls` for each created clip.

    License enforcement mirrors the management command (closes the gap noted
    in docs/EXPLAIN/scraping/03-licensing-safety.md): items whose license
    family does not permit commercial use are skipped unless allow_nc=True.
    CC-BY-SA items are imported with requires_share_alike=True and
    moderation_approved=False (model default), so they require operator
    approval via /clips/{id}/approve-moderation/ before reaching feeds.
    """
    from ai_ml.scrapers.sources import SOURCES
    from ai_ml.scrapers.base import (
        normalize_license,
        license_features,
        license_allows_commercial,
        is_share_alike_license,
    )
    from django.conf import settings as dj_settings
    module = SOURCES.get(source_name)
    if not module:
        raise RuntimeError(f"Unknown source: {source_name}")

    from django.contrib.auth import get_user_model
    UserModel = get_user_model()
    user = UserModel.objects.filter(is_superuser=True).first()
    if not user:
        user = UserModel.objects.create_user(username='scraper')
        user.set_unusable_password()
        user.save()

    # Honor explicit overrides; else fall back to env-driven settings.
    if allow_nc is None:
        allow_nc = getattr(dj_settings, 'SCRAPER_ALLOW_NC', False)
    if include_share_alike is None:
        include_share_alike = getattr(dj_settings, 'SCRAPER_ALLOW_SHARE_ALIKE', False)

    from ai_ml.scrapers import downloader, normalizer, uploader

    items = module.fetch_audio(limit=limit)
    imported = 0
    skipped = 0
    for item in items:
        url = item.get('url')
        title = item.get('title') or 'scraped audio'
        page = item.get('page_url') or ''
        lic_raw = item.get('license')
        original_id = item.get('id')
        family = normalize_license(lic_raw)
        nc, sa = license_features(family)
        nc = nc or bool(item.get('is_noncommercial'))
        if not license_allows_commercial(family, allow_nc=allow_nc):
            logger.info("scrape_and_import: skipping %s license=%s family=%s",
                        url, lic_raw, family)
            skipped += 1
            continue
        sa = sa or is_share_alike_license(family)

        local_input = None
        tmp_out = None
        try:
            if url.startswith('file://'):
                local_input = url[len('file://'):]
            else:
                local_input = downloader.download_audio(url)

            tmp_out = tempfile.NamedTemporaryFile(delete=False, suffix='.mp3').name
            normalizer.normalize_and_trim(local_input, tmp_out, max_seconds=clip_length, target_format='mp3')

            clip = uploader.save_clip(
                user=user,
                title=title,
                source_name=source_name,
                source_url=page,
                license=lic_raw or 'unknown',
                attribution_text=page,
                local_file_path=tmp_out,
                original_source_id=original_id,
                is_noncommercial=nc,
                requires_share_alike=sa,
                license_family=family,
            )

            publish(process_audio_to_hls, str(clip.id))
            imported += 1
            logger.info("Imported clip %s from %s (family=%s nc=%s sa=%s)",
                        clip.id, source_name, family, nc, sa)

        except Exception as e:
            logger.error("Failed to import %s: %s", url, e)

        finally:
            # local_input/tmp_out are always tempfile-backed local scratch
            # paths here (never the durable store — see uploader.save_clip,
            # which already writes through default_storage), so there's
            # nothing to protect against deleting; clean up unconditionally.
            for p in (local_input, tmp_out):
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except Exception as e:
                    logger.error("Failed to clean up temp file %s: %s", p, e)

    logger.info("scrape_and_import(%s): imported=%d skipped=%d allow_nc=%s sa=%s",
                source_name, imported, skipped, allow_nc, include_share_alike)


# ---------------------------------------------------------------------------
# Feed candidate pool (Redis pre-computation) tasks moved to
# ai_ml.pipelines.feed_tasks. They are re-exported via the shim
# at the top of this file so `from backend.app.tasks import
# rebuild_global_exploit_pool` (and the two siblings) still resolves.
# CELERY_BEAT_SCHEDULE and CELERY_TASK_ROUTES in
# backend/EchoFlow/settings.py key off the original task names,
# which are pinned in the new module with
# `name='backend.app.tasks.<func>'`.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Group B item 12: periodic cleanup of orphan HLS objects.
#
# Background: signals.cleanup_audioclip_storage removes the HLS tree
# on every AudioClip delete. If the S3 delete fails (network, creds,
# bucket perm), the failure is swallowed (WARNING log) and the orphan
# files persist forever. This task is the periodic safety net.
#
# How it works:
# 1. listdir('hls/') returns the immediate child names. These are the
#    clip_ids of the HLS trees.
# 2. Filter to UUID-shaped names (defense-in-depth: a stray non-UUID
#    directory in hls/ would never be in the AudioClip table anyway,
#    so we skip them rather than delete them).
# 3. Diff against AudioClip.objects.values_list('id', flat=True).
# 4. Bounded to max_keys per run. If the bucket has more orphans than
#    max_keys, the next day's run picks up the rest. This is the
#    back-pressure: a runaway orphan situation cannot page the
#    operator.
# 5. Idempotent: re-running is safe. A second run finds zero orphans
#    because the first run deleted them.
#
# Race condition: cleanup_orphan_hls vs post_delete signal. The signal
# fires after the row is deleted; the orphan scan runs at 03:00 UTC.
# The window where both could try to delete the same prefix is
# negligible (signal runs in ms, scan runs once a day). If a signal
# deletes the prefix between the listdir and the per-prefix delete,
# listdir(prefix) returns empty, the for loop is a no-op.
# ---------------------------------------------------------------------------
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


@shared_task(name='backend.app.tasks.cleanup_orphan_hls')
def cleanup_orphan_hls(max_keys: int = 1000) -> dict:
    """Scan hls/ prefix; delete prefixes whose clip_id is not in DB.

    Returns a dict with scanned and deleted counts. Logs a WARNING
    if scanned == max_keys (suggests more orphans than fit in one
    run; the next run will pick up the rest).
    """
    from . import metrics

    if not hasattr(default_storage, 'listdir'):
        logger.warning(
            "cleanup_orphan_hls: default_storage has no listdir(); "
            "skipping (this storage backend is not listable). "
            "If using a real S3 backend, ensure boto3 is installed."
        )
        return {'scanned': 0, 'deleted': 0, 'skipped': 'no_listdir'}

    try:
        # listdir returns (dirs, files) at the given path; we only
        # care about the dirs (the clip_id subdirs under hls/).
        hls_top_level, _files = default_storage.listdir('hls')
    except FileNotFoundError:
        # InMemoryStorage and some S3-compatible backends raise on
        # a missing prefix instead of returning empty. Treat as
        # "nothing to clean" — this is a no-op, not an error.
        return {'scanned': 0, 'deleted': 0}
    except Exception as exc:
        logger.warning("cleanup_orphan_hls: listdir('hls/') failed: %s", exc)
        return {'scanned': 0, 'deleted': 0, 'skipped': 'listdir_failed'}

    if not hls_top_level:
        return {'scanned': 0, 'deleted': 0}

    candidate_ids = [name for name in hls_top_level if _UUID_RE.match(name)]
    if not candidate_ids:
        return {'scanned': 0, 'deleted': 0}

    bounded = candidate_ids[:max_keys]
    if len(candidate_ids) > max_keys:
        logger.warning(
            "cleanup_orphan_hls: found %d candidate prefixes but "
            "bounded to %d; next run will pick up the rest",
            len(candidate_ids), max_keys,
        )

    existing_clip_ids = set(
        AudioClip.objects.filter(
            id__in=[uuid.UUID(s) for s in bounded],
        ).values_list('id', flat=True)
    )

    # DECISION: inline the prefix-deletion logic here rather than
    # calling signals._delete_s3_prefix, which captures its own
    # default_storage at import time. The task runs in a separate
    # process (celery_media) and may be tested with a fake storage;
    # inline keeps the storage reference dynamic. 6 lines of
    # duplication, but they live next to the code that calls them.
    def _delete_prefix(prefix: str) -> bool:
        """Delete all files under prefix. Returns True if any file
        was actually deleted; False if the prefix was empty or missing.
        """
        try:
            _dirs, files = default_storage.listdir(prefix)
        except FileNotFoundError:
            # Already deleted (or never existed); nothing to do.
            return False
        except Exception as exc:
            logger.warning(
                "cleanup_orphan_hls: listdir(%s) failed: %s", prefix, exc,
            )
            return False
        if not files:
            return False
        for fname in files:
            key = f"{prefix}/{fname}".replace(os.sep, '/')
            try:
                default_storage.delete(key)
            except Exception as exc:
                logger.warning(
                    "cleanup_orphan_hls: delete(%s) failed: %s", key, exc,
                )
        return True

    deleted = 0
    for orphan_id in bounded:
        try:
            if uuid.UUID(orphan_id) in existing_clip_ids:
                continue
            if _delete_prefix(f'hls/{orphan_id}'):
                metrics.orphan_hls_cleaned_total.inc()
                deleted += 1
        except Exception as exc:
            logger.warning(
                "cleanup_orphan_hls: failed to delete hls/%s: %s",
                orphan_id, exc,
            )

    logger.info(
        "cleanup_orphan_hls: scanned=%d, deleted=%d (bounded at %d)",
        len(bounded), deleted, max_keys,
    )
    return {'scanned': len(bounded), 'deleted': deleted}


# ---------------------------------------------------------------------------
# Periodic counter flusher — event-driven metrics pipeline.
#
# Background: UserInteraction.save() (in models.py) writes counter
# deltas to Redis (counter_store.increment) for the simple clip-global
# counters (likes/shares/skips). The synchronous UserInteraction F()
# side-effect has been removed; this task is the only path from
# Redis to Postgres for those counters AND for the per-(user,clip)
# completion accumulator. It runs every 5 minutes via Celery Beat.
#
# Four responsibilities, applied in order:
#
#   1. Counter deltas (likes/shares/skips): one F() UPDATE per dirty
#      clip. A clip with 1000 likes in 5 min gets ONE row lock, not
#      1000. This collapses the prior row-lock contention that
#      motivated the event-driven migration.
#
#   2. avg_completion_rate: per-(user,clip) completion samples are
#      drained as (sum, count) pairs. The flusher divides to recover
#      the mean and applies it to AudioClip.avg_completion_rate. NO
#      correlated subquery on userinteraction (the audit-flagged
#      pathology that originally motivated this rewrite).
#
#   3. engagement_velocity: computed in Python from the post-UPDATE
#      likes and shares values, applied to the SAME dirty rows we
#      already touched for the counter delta. Replaces the legacy
#      `update_global_metrics` raw-SQL pass that scanned every ready
#      clip every 5 minutes.
#
#   4. UserInteraction row materialization: per-(user,clip)
#      completion samples also drive a single bulk_create of
#      UserInteraction(interaction_type='view') rows, preserving
#      the row shape that downstream consumers (e.g. the
#      recommendation engine) used to read synchronously.
#
# DECISION: one Celery beat entry covers all four responsibilities;
# the task touches only dirty rows. No full-table scan.
# ---------------------------------------------------------------------------
@shared_task(name='backend.app.tasks.flush_counters_to_pg')
def flush_counters_to_pg(batch_size: int = 500) -> dict:
    """Drain Redis counter deltas; apply to Postgres.

    Returns: {
        'drained': N,
        'applied_counters': M,
        'applied_completion': K,
        'applied_velocity': V,
        'materialized_rows': R,
    }
    """
    from .services import counter_store

    drained = counter_store.drain()
    counter_deltas: dict = drained.get('counters', {})
    completion_deltas: dict = drained.get('completion', {})
    drained_count = sum(len(v) for v in counter_deltas.values()) + sum(
        1 for _ in completion_deltas
    )

    if not counter_deltas and not completion_deltas:
        return {
            'drained': 0,
            'applied_counters': 0,
            'applied_completion': 0,
            'applied_velocity': 0,
            'materialized_rows': 0,
        }

    dirty_clip_ids = _collect_dirty_clip_ids(counter_deltas, completion_deltas)
    applied_counters = _apply_counter_deltas(counter_deltas, batch_size)
    applied_completion, materialized_rows = _apply_completion_deltas(
        completion_deltas, batch_size,
    )
    applied_velocity = _apply_engagement_velocity(
        dirty_clip_ids, batch_size,
    )

    logger.info(
        "flush_counters_to_pg: drained=%d applied_counters=%d applied_completion=%d applied_velocity=%d materialized_rows=%d",
        drained_count, applied_counters, applied_completion, applied_velocity, materialized_rows,
    )
    return {
        'drained': drained_count,
        'applied_counters': applied_counters,
        'applied_completion': applied_completion,
        'applied_velocity': applied_velocity,
        'materialized_rows': materialized_rows,
    }


def _collect_dirty_clip_ids(
    counter_deltas: dict[str, dict[str, int]],
    completion_deltas: dict[tuple[str, str], dict[str, float]],
) -> list[str]:
    """Union of clip_ids that had ANY dirty state this beat.

    engagement_velocity is recomputed for these clips only (the
    legacy update_global_metrics scanned every ready clip; we now
    scan only the dirty set).
    """
    dirty: set[str] = set(counter_deltas.keys())
    for clip_id, _user_id in completion_deltas:
        dirty.add(clip_id)
    return list(dirty)


def _apply_counter_deltas(
    counter_deltas: dict[str, dict[str, int]], batch_size: int,
) -> int:
    """Apply likes/shares/skips F() updates. One UPDATE per dirty clip.

    A clip with all three deltas gets a single row lock + single
    UPDATE. The previous O(N) per-event approach serialized 1000
    likes behind 1000 row locks; this collapses to 1.
    """
    if not counter_deltas:
        return 0
    items = list(counter_deltas.items())[:batch_size]
    applied = 0
    for clip_id, counter_deltas_per_clip in items:
        if not counter_deltas_per_clip:
            continue
        try:
            from django.db.models import F as _F
            expr = {
                ct: _F(ct) + delta
                for ct, delta in counter_deltas_per_clip.items()
            }
            AudioClip.objects.filter(pk=clip_id).update(**expr)
            applied += 1
        except Exception as exc:
            logger.warning(
                "flush_counters_to_pg: counter update failed for %s: %s",
                clip_id, exc,
            )
    return applied


def _apply_completion_deltas(
    completion_deltas: dict[tuple[str, str], dict[str, float]],
    batch_size: int,
) -> tuple[int, int]:
    """Apply completion-rate deltas to AudioClip.avg_completion_rate and
    materialize a UserInteraction row per (user, clip) per beat.

    Returns: (clip_updates_applied, rows_materialized)
    """
    if not completion_deltas:
        return 0, 0

    # Aggregate per-clip mean completion rate. Multiple (user,clip)
    # samples in the same beat get weighted-averaged into one
    # AudioClip.avg_completion_rate value per clip. The aggregation
    # is total_sum / total_count over the drained samples.
    per_clip_sum: dict[str, float] = {}
    per_clip_count: dict[str, int] = {}
    for (clip_id, _user_id), slot in completion_deltas.items():
        s = float(slot.get('completion_sum', 0.0))
        c = int(slot.get('completion_count', 0))
        if c <= 0:
            continue
        per_clip_sum[clip_id] = per_clip_sum.get(clip_id, 0.0) + s
        per_clip_count[clip_id] = per_clip_count.get(clip_id, 0) + c

    # Apply per-clip avg_completion_rate with one UPDATE per dirty clip.
    applied = 0
    for clip_id, total_count in list(per_clip_count.items())[:batch_size]:
        if total_count <= 0:
            continue
        mean = per_clip_sum[clip_id] / total_count
        try:
            AudioClip.objects.filter(pk=clip_id).update(avg_completion_rate=mean)
            applied += 1
        except Exception as exc:
            logger.warning(
                "flush_counters_to_pg: completion update failed for %s: %s",
                clip_id, exc,
            )

    # Materialize one UserInteraction row per (user, clip) per beat.
    # The unique_together (user, clip, interaction_type) constraint
    # is satisfied by using a fixed action_type='view' for the
    # aggregated completion row. The row's completion_rate is the
    # weighted mean across the drained samples for that (user, clip).
    materialized = _materialize_user_interaction_rows(
        completion_deltas, batch_size,
    )
    return applied, materialized


def _apply_engagement_velocity(
    dirty_clip_ids: list[str], batch_size: int,
) -> int:
    """Recompute engagement_velocity for dirty clips.

    The legacy `update_global_metrics` scanned every ready clip
    every 5 minutes and recomputed engagement_velocity from
    `likes + (shares * 2)` re-read from each row. With the flusher
    already applying the F() deltas, the post-state values are
    available in this Python process; we read them in a single
    `WHERE id = ANY(%s)` query and UPDATE the same set in one pass.

    Formula (matches the legacy raw SQL exactly):
        LEAST((likes + (shares * 2))
              / POWER(EXTRACT(EPOCH FROM (NOW() - created_at))/3600.0 + 2.0, 1.5)
              / 100.0,
              1.0)

    DECISION: rather than re-implementing the math in Python and
    suffering floating-point drift, we let Postgres do the math
    with a CASE expression. One UPDATE statement covers all dirty
    clips in a single round-trip, with the formula preserved 1:1
    against the legacy SQL.
    """
    if not dirty_clip_ids:
        return 0
    bounded = dirty_clip_ids[:batch_size]
    import uuid as _uuid
    try:
        uuid_objs = [_uuid.UUID(str(c)) for c in bounded]
    except (TypeError, ValueError):
        logger.warning(
            "flush_counters_to_pg: invalid clip_id in engagement_velocity pass"
        )
        return 0

    # BATCHED UPDATE: one statement for the entire dirty set.
    # The formula mirrors the legacy raw SQL with NOW() and
    # EXTRACT(EPOCH FROM (NOW() - created_at)) replicated.
    #
    # SECURITY: table name comes from Django's _meta so the SQL
    # is bound to the current model — not a string-concat with
    # user input. The DB engine detection gates the Postgres-only
    # `%s::uuid[]` cast and `EXTRACT(EPOCH FROM ...)` function;
    # on SQLite (the test engine) we fall back to a portable
    # form that exercises the same path.
    table = AudioClip._meta.db_table
    db_engine = connection.settings_dict.get('ENGINE', '')
    if 'postgresql' in db_engine:
        sql = f"""
            UPDATE {table}
            SET engagement_velocity = LEAST(
                (likes + (shares * 2))
                / POWER(EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600.0 + 2.0, 1.5)
                / 100.0,
                1.0
            )
            WHERE id = ANY(%s::uuid[]) AND status = 'ready'
        """
        params = [uuid_objs]
    else:
        # Portable form for SQLite (the test fixture's engine).
        # SQLite uses MIN() instead of LEAST() and strftime('%s', ...)
        # for the seconds-since-epoch delta. The math is identical to
        # the Postgres path; production runs on Postgres so the
        # Postgres branch above is the source of truth.
        #
        # DECISION: UUIDField on SQLite stores the compact hex form
        # (no dashes). Postgres compares against the dashed form.
        # We pass the compact form here to keep the WHERE-clause
        # correct on the test engine.
        sql = f"""
            UPDATE {table}
            SET engagement_velocity = MIN(
                (likes + (shares * 2))
                / POWER(
                    (strftime('%s', 'now') - strftime('%s', created_at)) / 3600.0 + 2.0,
                    1.5
                ) / 100.0,
                1.0
            )
            WHERE id IN ({','.join('?' * len(uuid_objs))}) AND status = 'ready'
        """
        params = [u.hex for u in uuid_objs]

    try:
        with connection.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount
    except Exception as exc:
        logger.warning(
            "flush_counters_to_pg: engagement_velocity update failed: %s",
            exc,
        )
        return 0


def _materialize_user_interaction_rows(
    completion_deltas: dict[tuple[str, str], dict[str, float]],
    batch_size: int,
) -> int:
    """Bulk-create one UserInteraction row per drained (user, clip) tuple.

    Each (user, clip) bucket from the drain becomes exactly one
    UserInteraction row with interaction_type='view' and the
    aggregated completion_rate. We use update_or_create so a
    re-flush of the same (user, clip) within the same beat is
    idempotent (the unique_together constraint would otherwise
    fail).

    The clip-global avg_completion_rate was already applied in
    _apply_completion_deltas; this only materializes the per-user
    row so the recommendation engine can read its existing
    UserInteraction-shaped inputs.
    """
    if not completion_deltas:
        return 0

    from .models import UserInteraction
    from django.contrib.auth import get_user_model

    UserModel = get_user_model()
    items = list(completion_deltas.items())[:batch_size]
    if not items:
        return 0

    # Batch-resolve the FKs. The keys here are stringified UUIDs and
    # stringified user IDs (the counter_store keys are uniformly
    # string-coerced via `str(...)` at the call sites).
    clip_ids: set = set()
    user_id_strs: set[str] = set()
    for (clip_id, user_id), _slot in items:
        clip_ids.add(clip_id)
        user_id_strs.add(user_id)

    try:
        import uuid as _uuid
        uuid_clip_ids = {_uuid.UUID(str(c)) for c in clip_ids}
        clips_by_id = AudioClip.objects.in_bulk(uuid_clip_ids)
    except (TypeError, ValueError):
        logger.warning("flush_counters_to_pg: invalid clip_id in completion drain")
        return 0
    try:
        int_user_ids = {int(u) for u in user_id_strs}
    except (TypeError, ValueError):
        int_user_ids = set()
    try:
        users_by_id = UserModel.objects.in_bulk(int_user_ids) if int_user_ids else {}
    except Exception as exc:
        logger.warning("flush_counters_to_pg: user in_bulk failed: %s", exc)
        users_by_id = {}

    written = 0
    for (clip_id, user_id), slot in items:
        # Resolve clip.
        try:
            import uuid as _uuid
            clip_obj = clips_by_id.get(_uuid.UUID(str(clip_id)))
        except (TypeError, ValueError, KeyError):
            continue
        if clip_obj is None:
            continue
        try:
            user_obj = users_by_id.get(int(user_id))
        except (TypeError, ValueError):
            user_obj = None
        if user_obj is None:
            continue
        s = float(slot.get('completion_sum', 0.0))
        c = int(slot.get('completion_count', 0))
        if c <= 0:
            continue
        mean = s / c
        try:
            UserInteraction.objects.update_or_create(
                user=user_obj,
                clip=clip_obj,
                interaction_type='view',
                defaults={
                    'completion_rate': mean,
                    'watch_time_ms': 0,
                    'is_active': True,
                },
            )
            written += 1
        except Exception as exc:
            logger.warning(
                "flush_counters_to_pg: materialize row failed for clip=%s user=%s: %s",
                clip_id, user_id, exc,
            )
    return written


