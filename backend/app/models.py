import os
import uuid
import logging
from django.db import models, transaction
from django.db.models import F
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from pgvector.django import VectorField
from pgvector.django import HnswIndex

logger = logging.getLogger(__name__)


class User(AbstractUser):
    # ISSUE-01: DPDP consent / age gate fields
    dob = models.DateField(null=True, blank=True)
    is_minor = models.BooleanField(default=False)
    minor_consent_verified = models.BooleanField(default=False)
    consent_accepted = models.BooleanField(default=False)
    terms_version = models.CharField(max_length=50, blank=True, default='')
    parent_email = models.EmailField(blank=True, null=True)

    @property
    def computed_is_minor(self) -> bool:
        # DECISION: Compute minor status from dob rather than relying
        # solely on the stored bool — prevents drift if dob changes.
        # Tradeoff: small CPU cost per access vs. data consistency.
        if not self.dob:
            return False
        from datetime import date
        today = date.today()
        age = today.year - self.dob.year - ((today.month, today.day) < (self.dob.month, self.dob.day))
        return age < 18

    # N3 fix: encrypted_email removed. The previous design encrypted
    # plaintext email on save and stored it in a TextField with unique=True,
    # but: (a) nothing ever decrypted it (no lookup-by-email, no password
    # reset, no admin view), (b) Fernet is non-deterministic (random IV per
    # encrypt) so the unique=True constraint never fired for actual duplicate
    # emails, (c) TagsViewSet.initialize_vectors called user.save() on every
    # vector update, re-encrypting the email each time (wasted UPDATE), and
    # (d) plaintext AbstractUser.email is what RegisterSerializer validates
    # against via UniqueValidator — the encrypted column was misleading
    # theatre that provided zero security benefit.
    #
    # The plaintext email field (inherited from AbstractUser) is the source
    # of truth. It is unique=True at the DB level via Django's auto-generated
    # constraint, and RegisterSerializer's UniqueValidator catches duplicates
    # at the API boundary. For GDPR/privacy, the answer is to use a real
    # encryption-at-rest strategy (column encryption via RDS, or
    # deterministic encryption with HMAC for lookup) — not random-IV Fernet.
    following = models.ManyToManyField('self', symmetrical=False, related_name='followers', blank=True)
    long_term_semantic = VectorField(dimensions=384, null=True, blank=True)
    long_term_acoustic = VectorField(dimensions=128, null=True, blank=True)
    profile_picture = models.ImageField(upload_to='avatars/', null=True, blank=True)




class ConsentAudit(models.Model):
    # ISSUE-01 (DPDP consent / age gate)
    # DECISION: DB table for consent audit trail rather than file-based
    # audit logs — queryable by user, withdrawable, and retainable
    # per regulatory timeline. Tradeoff: extra table + index vs.
    # tamper-resistant DB record.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='consent_audits', null=True, blank=True)
    consent_issued_at = models.DateTimeField(auto_now_add=True)
    terms_version_id = models.CharField(max_length=50, default='v1.0')
    privacy_version_id = models.CharField(max_length=50, default='v1.0')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-consent_issued_at']
        indexes = [models.Index(fields=['user', '-consent_issued_at'])]

class AudioClip(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='audio_clips')
    title = models.CharField(max_length=255)
    category = models.CharField(max_length=50, blank=True)
    
    original_file = models.FileField(upload_to='uploads/%Y/%m/%d/', null=True)
    cover_image = models.ImageField(upload_to='covers/%Y/%m/%d/', blank=True, null=True)
    hls_playlist_url = models.CharField(max_length=500, blank=True, null=True)
    # Provenance and licensing metadata for scraper imports
    source_name = models.CharField(max_length=100, blank=True, null=True)
    source_url = models.CharField(max_length=500, blank=True, null=True)
    license = models.CharField(max_length=100, blank=True, null=True)
    attribution_text = models.CharField(max_length=500, blank=True, null=True)
    imported_via_scraper = models.BooleanField(default=False)
    original_source_id = models.CharField(max_length=255, blank=True, null=True)
    # DECISION: Two boolean fields instead of a license-policy table so feed
    # queries can filter NC + SA with index-friendly predicates. Populated by
    # uploader.save_clip() via ai_ml.scrapers.base.license_features().
    # SECURITY: is_noncommercial=True clips are excluded from feed/suggestions
    # queries until SCRAPER_ALLOW_NC=True (operator opt-in).
    is_noncommercial = models.BooleanField(default=False)
    requires_share_alike = models.BooleanField(default=False)
    license_family = models.CharField(max_length=32, blank=True, default='')
    
    # Global Metrics & Telemetry Context
    duration_ms = models.IntegerField(default=0) 
    avg_completion_rate = models.FloatField(default=0.0) 
    engagement_velocity = models.FloatField(default=0.0) 
    
    likes = models.BigIntegerField(default=0)
    shares = models.BigIntegerField(default=0)
    skips = models.BigIntegerField(default=0)
    comment_count = models.BigIntegerField(default=0)
    # DECISION: Added CheckConstraint in Meta to enforce non-negative
    # counters at DB level, protecting against raw SQL and ORM updates. 
    # Tradeoff: Slightly stricter DB writes vs. guaranteed data integrity.
    
    # AI Intelligence (vibe_vector completely removed)
    tags = models.JSONField(default=list, blank=True)
    #semantic_vector = VectorField(dimensions=1536, null=True, blank=True)
    semantic_vector = VectorField(dimensions=384, null=True, blank=True)
    acoustic_vector = VectorField(dimensions=128, null=True, blank=True)

    moderation_approved = models.BooleanField(default=False)
    copyright_acknowledgement = models.BooleanField(default=False)
    copyright_owner_name = models.CharField(max_length=255, blank=True, null=True)

    # DECISION: Group ID + segment index for the "split long audio into
    # N pieces" workflow. group_id is a UUID shared across all segments
    # of one source item; NULL when this clip is a single (no-split)
    # item. segment_index is 0-based; segment_count is the total N
    # segments in the group (denormalized for fast "show all parts of
    # this clip" queries without a JOIN). One AudioClip row per
    # segment. The original uploader used to truncate a 4h LibriVox
    # audiobook to 5min; the new uploader splits and saves all N
    # pieces. group_id + segment_index let the feed/show pages list
    # "part 1 of 7" without a separate Segment table.
    group_id = models.UUIDField(null=True, blank=True, db_index=True)
    segment_index = models.IntegerField(null=True, blank=True)
    segment_count = models.IntegerField(null=True, blank=True)
    license_type = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=20, default='processing')
    created_at = models.DateTimeField(auto_now_add=True)
    def __str__(self):
        return f"{self.title} by {self.creator.username}"
    class Meta:
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['status', '-engagement_velocity']),
            models.Index(fields=['category', '-likes']),
            HnswIndex(
                name='semantic_vector_index',
                fields=['semantic_vector'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops']
            ),

            # Repeat for acoustic_vector
            HnswIndex(
                name='acoustic_vector_index',
                fields=['acoustic_vector'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops']
            ),
            # SECURITY: index supports fast feed-exclusion of NC + SA items.
            # Created in migration 0002; declared here so Django's
            # auto-discovery matches the live DB schema and doesn't emit
            # a spurious "remove index" migration.
            models.Index(fields=['is_noncommercial', '-created_at'],
                         name='audioclip_nc_created_idx'),
            models.Index(fields=['requires_share_alike', '-created_at'],
                         name='audioclip_sa_created_idx'),
        ]
        # DECISION: DB-level constraints prevent negative counter values
        # even via raw SQL or ORM bulk updates. Tradeoff: Migration required.
        constraints = [
            models.CheckConstraint(check=models.Q(likes__gte=0), name='likes_non_negative'),
            models.CheckConstraint(check=models.Q(shares__gte=0), name='shares_non_negative'),
            models.CheckConstraint(check=models.Q(skips__gte=0), name='skips_non_negative'),
            models.CheckConstraint(check=models.Q(comment_count__gte=0), name='comment_count_non_negative'),
        ]

class Comment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clip = models.ForeignKey('AudioClip', on_delete=models.CASCADE, related_name='comments')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='replies')
    text = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['clip', '-created_at'])]
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        # NOTE: use _state.adding, NOT `not self.pk` — UUID pks with a callable
        # default are assigned at __init__, so self.pk is never None on create.
        if self._state.adding and not self.parent_id:
            AudioClip.objects.filter(pk=self.clip_id).update(comment_count=F('comment_count') + 1)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if not self.parent_id:
            AudioClip.objects.filter(pk=self.clip_id).update(comment_count=F('comment_count') - 1)
        super().delete(*args, **kwargs)

class ShareEvent(models.Model):
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='sent_shares', on_delete=models.CASCADE)
    receiver = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='received_shares', on_delete=models.CASCADE)
    clip = models.ForeignKey(AudioClip, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=['receiver', '-created_at', 'is_read'])]

class UserInteraction(models.Model):
    TYPES = [
        ('like', 'Like'),
        ('share', 'Share'),
        ('skip', 'Skip'),
        ('view', 'View') # Added to track explicit views/completions
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    clip = models.ForeignKey(AudioClip, on_delete=models.CASCADE)
    interaction_type = models.CharField(max_length=10, choices=TYPES)
    
    # New fields to fix re-likes and track completion
    is_active = models.BooleanField(default=True)
    #created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    watch_time_ms = models.IntegerField(default=0)
    completion_rate = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'clip', 'interaction_type')
        indexes = [models.Index(fields=['user', 'interaction_type'])]

    def save(self, *args, **kwargs):
        # Event-driven metrics pipeline (Group B item 9, completed
        # 2026-09). The legacy F() counter side-effect on
        # AudioClip.likes/shares/skips has been removed; the
        # flush_counters_to_pg task is the only path from Redis to
        # Postgres for the denormalized counters. This save() now
        # only does:
        #
        #   1. Lock the UserInteraction row to resolve concurrent
        #      toggle-like requests without double-counting the
        #      Redis INCRBY delta (N2 fix preserved).
        #   2. Compute increment_val from the state-change diff.
        #   3. Fire the counter_store.increment() Redis write
        #      (fire-and-forget; logged at DEBUG on failure).
        #
        # The check_constraint negative-floor and the
        # atomic-block boundary stay; the F() UPDATE on
        # AudioClip is gone.
        is_new = self._state.adding
        state_changed = False
        increment_val = 0
        counter_type = None

        with transaction.atomic():
            if is_new:
                state_changed = True
                increment_val = 1 if self.is_active else 0
            else:
                # Lock the row to prevent concurrent saves from
                # double-counting the Redis INCRBY.
                old_instance = UserInteraction.objects.select_for_update().get(pk=self.pk)
                if old_instance.is_active != self.is_active:
                    state_changed = True
                    increment_val = 1 if self.is_active else -1

            super().save(*args, **kwargs)

            if state_changed and increment_val != 0:
                from .services import counter_store
                _field_map = {'like': 'likes', 'share': 'shares', 'skip': 'skips'}
                counter_type = _field_map.get(self.interaction_type)
                if counter_type:
                    try:
                        counter_store.increment(
                            str(self.clip.pk), counter_type, increment_val,
                        )
                    except Exception as exc:
                        # SECURITY: never let a metrics/counter hook
                        # break the user-facing write. The counter
                        # is observability; losing a single sample
                        # is acceptable and the flusher will
                        # reconcile on the next beat.
                        logger.debug(
                            "counter_store.increment failed for %s/%s: %s",
                            self.clip.pk, counter_type, exc,
                        )

class Grievance(models.Model):
    # ISSUE-03 (grievance / compliance) — DB table per audit recommendation.
    # DECISION: DB table rather than env-only config for queryability
    # and audit retention.
    subject = models.CharField(max_length=200)
    description = models.TextField()
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='grievances')
    user_email = models.EmailField(blank=True, null=True)
    status = models.CharField(max_length=20, default='received', choices=[
        ('received', 'Received'),
        ('acknowledged', 'Acknowledged'),
        ('under_review', 'Under Review'),
        ('resolved', 'Resolved'),
    ])
    acknowledgment_due = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', '-created_at'])]

class AuditLog(models.Model):
    # ISSUE-07 (audit identity retention)
    # DECISION: Every request logs user, endpoint, IP, UA, correlation_id.
    # Tradeoff: DB write overhead per request vs. complete audit trail.
    # HACK: Using generic endpoint char field rather than full URL to
    # limit PII exposure in audit table (path only, no query params).
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    action = models.CharField(max_length=50, choices=[
        ('view', 'View'), ('create', 'Create'), ('update', 'Update'),
        ('delete', 'Delete'), ('login', 'Login'), ('register', 'Register'),
    ])
    endpoint = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    correlation_id = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['user', '-timestamp']),
            models.Index(fields=['correlation_id',]),
        ]

class DataSubjectRequest(models.Model):
    # ISSUE-06 (data-subject rights / GDPR-style access/erasure)
    request_type = models.CharField(max_length=20, choices=[
        ('access', 'Access'), ('erasure', 'Erasure'),
    ])
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='data_subject_requests')
    status = models.CharField(max_length=20, default='pending')
    token_hash = models.CharField(max_length=128, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cooling_off_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', 'request_type'])]

class TakedownRequest(models.Model):
    # ISSUE-03 / content moderation
    clip = models.ForeignKey('AudioClip', on_delete=models.CASCADE, related_name='takedown_requests')
    reason = models.TextField()
    requester_email = models.EmailField()
    status = models.CharField(max_length=20, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

class Report(models.Model):
    # General regulatory reporting / audit artifact
    title = models.CharField(max_length=200)
    content = models.TextField()
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='reports')
    status = models.CharField(max_length=20, default='open')
    created_at = models.DateTimeField(auto_now_add=True)

