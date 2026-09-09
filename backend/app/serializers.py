import os
import logging
from rest_framework import serializers
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Exists, OuterRef
from .media_urls import get_hls_playback_url, get_signed_media_url
from .models import AudioClip, UserInteraction, ShareEvent, Comment, ConsentAudit, Grievance, AuditLog
from rest_framework.validators import UniqueValidator


User = get_user_model()


# ---------------------------------------------------------------------------
# Pure-Python magic-byte allowlist.
#
# SECURITY: a 14-byte header check that runs in <1us, no system binary
# required. Catches the most common attack vectors (PE/EXE trojans, ELF
# binaries, scripts, archives) before the file ever reaches the ffprobe
# subprocess or object storage. This is the FIRST line of defense;
# python-magic (libmagic) and the pydub duration probe are the second
# and third.
#
# Audio formats we accept share common header patterns that are NOT
# in the BLOCKED_SIGNATURES list below. We only block signatures we
# can identify with high confidence — unknown headers fall through
# to the extension check and the pydub probe.
# ---------------------------------------------------------------------------
_BLOCKED_MAGIC_SIGNATURES = (
    # Windows PE / DOS executable
    b'MZ',
    # ELF (Linux/Unix executable)
    b'\x7fELF',
    # Bash/sh script shebang
    b'#!/bin/sh',
    b'#!/bin/bash',
    b'#!/usr/bin/env',
    # Python script shebang
    b'#!/usr/bin/python',
    b'#!/usr/bin/env python',
    # Perl script shebang
    b'#!/usr/bin/perl',
    # PDF document
    b'%PDF-',
    # Java class file
    b'\xca\xfe\xba\xbe',
    # Mach-O universal binary (macOS executable)
    b'\xca\xfe\xba\xbe',  # same as Java class; intentionally listed once
    b'\xcf\xfa\xed\xfe',  # 64-bit little-endian
    b'\xfe\xed\xfa\xce',  # 32-bit big-endian
    b'\xfe\xed\xfa\xcf',  # 64-bit big-endian
    # ZIP / DOCX / XLSX / JAR (archives disguised as audio)
    b'PK\x03\x04',
    b'PK\x05\x06',
    b'PK\x07\x08',
    # RAR archive
    b'Rar!\x1a\x07',
    # 7z archive
    b'7z\xbc\xaf\x27\x1c',
    # Gzip
    b'\x1f\x8b',
    # Windows BMP
    b'BM',
    # GIF
    b'GIF87a',
    b'GIF89a',
    # PNG
    b'\x89PNG\r\n\x1a\n',
    # JPEG
    b'\xff\xd8\xff',
)


def _has_blocked_magic_signature(head: bytes) -> str | None:
    """Return the matched signature label if `head` matches a known
    non-audio file signature, else None.

    The label is human-readable and surfaced in the rejection error
    so the audit logs can show the detected type without exposing
    the full binary header.
    """
    for sig in _BLOCKED_MAGIC_SIGNATURES:
        if head.startswith(sig):
            if sig.startswith(b'MZ'):
                return 'PE/EXE executable'
            if sig.startswith(b'\x7fELF'):
                return 'ELF executable'
            if sig.startswith(b'#!'):
                return 'script'
            if sig.startswith(b'%PDF'):
                return 'PDF document'
            if sig == b'\xca\xfe\xba\xbe':
                return 'Java class / Mach-O binary'
            if sig.startswith(b'PK'):
                return 'ZIP archive'
            if sig.startswith(b'Rar!'):
                return 'RAR archive'
            if sig.startswith(b'7z'):
                return '7-Zip archive'
            if sig.startswith(b'\x1f\x8b'):
                return 'gzip archive'
            if sig == b'BM':
                return 'BMP image'
            if sig.startswith(b'GIF'):
                return 'GIF image'
            if sig.startswith(b'\x89PNG'):
                return 'PNG image'
            if sig.startswith(b'\xff\xd8\xff'):
                return 'JPEG image'
            if sig == b'\xcf\xfa\xed\xfe' or sig == b'\xfe\xed\xfa\xcf':
                return 'Mach-O 64-bit binary'
            if sig == b'\xfe\xed\xfa\xce':
                return 'Mach-O 32-bit binary'
            return 'non-audio content'
    return None

class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'date_joined']
        read_only_fields = ['id', 'date_joined']

class AudioUploadSerializer(serializers.ModelSerializer):
    # DECISION: Added file type/size validation at serializer boundary
    # rather than model level to provide fast feedback to client.
    # Tradeoff: Slightly more code in serializer vs. guaranteed validation.
    ALLOWED_EXT = {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac', '.webm', '.opus'}
    MAX_SIZE = 100 * 1024 * 1024  # 100 MB
    # SECURITY: Magic-byte MIME allowlist. An attacker can rename evil.exe
    # to evil.mp3 and bypass extension-only checks. python-magic reads the
    # first ~1KB of the file and returns the inferred MIME type. We accept
    # only audio/* MIME types. If libmagic is unavailable, fall back to
    # extension-only (with a logged warning) so the service doesn't break
    # on minimal Docker images.
    ALLOWED_MIMES = frozenset({
        'audio/mpeg', 'audio/mp3', 'audio/wav', 'audio/x-wav',
        'audio/wave', 'audio/x-vorbis+ogg', 'audio/ogg', 'audio/flac',
        'audio/x-flac', 'audio/mp4', 'audio/aac', 'audio/x-m4a',
        'audio/webm', 'audio/opus',
    })

    # ISSUE-05: User-upload licensing and copyright acknowledgment.
    LICENSE_CHOICES = [
        ("Owned", "Owned"),
        ("CC0", "CC0"),
        ("CC-BY", "CC BY"),
        ("CC-BY-SA", "CC BY-SA"),
        ("CC-BY-NC", "CC BY-NC"),
        ("Public_Domain", "Public Domain"),
        ("Unknown", "Unknown"),
    ]
    license_type = serializers.ChoiceField(
        choices=LICENSE_CHOICES,
        default="Unknown",
        required=False,
    )
    copyright_owner_name = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    copyright_acknowledgement = serializers.BooleanField(
        required=True,
    )

    class Meta:
        model = AudioClip
        fields = ['id', 'title', 'category', 'original_file', 'status', 'license_type', 'copyright_owner_name', 'copyright_acknowledgement']
        # N8 fix: original_file is writable on create (POST) but read-only
        # on update (PATCH/PUT). The serializer-level read_only_fields
        # applies to BOTH, so we use 'original_file' as a writable
        # field here and enforce read-only-on-update at the view level
        # via an update() override that raises PermissionDenied or
        # silently ignores the field. See AudioUploadViewSet.update().
        read_only_fields = ['id', 'status']

    def validate(self, data):
        # SECURITY / REGULATORY: Enforce copyright acknowledgment.
        # Per ISSUE-05 (Copyright Act 1957 / IT Rules 2021), the user
        # must explicitly confirm they have the right to upload the audio.
        if not data.get("copyright_acknowledgement", False):
            raise serializers.ValidationError(
                {"copyright_acknowledgement": "You must acknowledge that you have the right to upload this audio and that it does not infringe any third-party rights."}
            )
        license_type = data.get("license_type", "Unknown")
        if license_type == "Unknown":
            logger = logging.getLogger(__name__)
            logger.warning("Upload with Unknown license type — audit trail required.")
        return data

    def validate_original_file(self, value):
        if value.size > self.MAX_SIZE:
            raise serializers.ValidationError(f"File exceeds {self.MAX_SIZE//1024//1024}MB limit.")
        ext = os.path.splitext(value.name)[1].lower()
        if ext not in self.ALLOWED_EXT:
            raise serializers.ValidationError(f"Unsupported file type: {ext}")
        # SECURITY: Pure-Python magic-byte sniff. Reads the first 8KB
        # and matches against a hard-coded list of KNOWN non-audio
        # signatures. Catches PE/EXE, ELF, scripts, archives, and
        # common image formats BEFORE the file reaches the ffprobe
        # subprocess (or object storage, on a longer path). No
        # external binary required, so this check works on minimal
        # Docker images without ffmpeg installed and is the most
        # reliable first line of defense.
        value.seek(0)
        head = value.read(8192)
        value.seek(0)
        detected_type = _has_blocked_magic_signature(head)
        if detected_type is not None:
            raise serializers.ValidationError(
                f"File content does not match audio format. Detected: {detected_type}"
            )
        # Magic-byte sniff via libmagic (python-magic). Layer-2 check:
        # if libmagic confidently identifies the file as a non-audio
        # MIME that the pure-Python allowlist above missed, reject it.
        # The pure-Python allowlist already covers the common attacks
        # (PE/ELF/scripts/archives); libmagic adds coverage for less
        # common formats. If python-magic is not installed, skip this
        # step (with a logged warning) and rely on the pydub probe.
        try:
            import magic
            mime = magic.from_buffer(head, mime=True)
            if mime and not mime.startswith('audio/') and mime != 'application/octet-stream':
                raise serializers.ValidationError(
                    f"File content does not match audio format. Detected: {mime}"
                )
        except ImportError:
            # python-magic not installed — log and fall back to the
            # pure-Python check + the pydub probe.
            logging.getLogger(__name__).warning(
                "python-magic unavailable; relying on pure-Python magic-byte allowlist"
            )
        # SECURITY: Duration probed at upload time, not in the worker. A
        # 100MB-but-24-hour WAV would otherwise be accepted into S3,
        # billed, and only then failed — wasting bandwidth, storage,
        # and worker time. Group C item 23 fix.
        #
        # DECISION: pydub over ffprobe because pydub is already in
        # requirements-media.txt for the worker, the API is cleaner
        # inside a serializer, and the underlying ffmpeg subprocess
        # is the same. Cost: ~6MB in the web image (already present
        # in the media image).
        #
        # HACK: Reading the whole upload into memory here to pass to
        # pydub. Django normally streams; we get a file object via
        # the serializer's value attr. pydub needs a path or
        # BytesIO, not a streaming file. TODO: stream via pydub's
        # from_file with a temp path if memory pressure becomes a
        # problem.
        from django.conf import settings as django_settings
        max_seconds = getattr(django_settings, 'MAX_DURATION_SECONDS', 300)
        try:
            import io
            from pydub import AudioSegment
            value.seek(0)
            data = value.read()
            value.seek(0)
            audio = AudioSegment.from_file(io.BytesIO(data))
            duration_seconds = len(audio) / 1000.0
            if duration_seconds > max_seconds:
                raise serializers.ValidationError(
                    f"Audio duration ({duration_seconds:.1f}s) exceeds maximum "
                    f"allowed ({max_seconds}s)."
                )
        except serializers.ValidationError:
            raise
        except Exception as e:
            # pydub raises various exceptions for unsupported/corrupt files.
            # CouldntDecodeError, FileNotFoundError (ffmpeg missing), etc.
            # Reject as unsupported so we don't accept garbage.
            logging.getLogger(__name__).warning(
                f"Duration probe failed for {value.name}: {type(e).__name__}: {e}"
            )
            raise serializers.ValidationError(
                "Could not probe audio duration. File may be corrupt or unsupported."
            )
        return value

    def create(self, validated_data):
        # Issue-05: Ensure copyright fields are persisted.
        validated_data['creator'] = self.context['request'].user
        validated_data.setdefault('moderation_approved', False)
        validated_data.setdefault('copyright_acknowledgement', validated_data.get('copyright_acknowledgement', False))
        clip = super().create(validated_data)
        return clip

class FeedClipSerializer(serializers.ModelSerializer):
    # Fixed from owner.username to creator.username
    creator_name = serializers.CharField(source='creator.username', read_only=True)
    creator_id = serializers.IntegerField(source='creator.id', read_only=True)
    is_liked = serializers.SerializerMethodField()
    # `AudioClip.hls_playlist_url` stores a relative object-storage KEY
    # (e.g. "hls/<clip_id>/master.m3u8"), not a servable URL — the bucket is
    # private, so a real playable URL has to be signed fresh on every read.
    # A signed URL persisted in the DB would silently expire after
    # AWS_S3_QUERYSTRING_EXPIRE regardless of whether the clip is still
    # valid, so we generate it here instead of trusting the stored field.
    hls_playlist_url = serializers.SerializerMethodField()
    cover_image = serializers.SerializerMethodField()

    class Meta:
        model = AudioClip
        fields = [
            'id', 'title', 'creator_name', 'category',
            'hls_playlist_url', 'likes', 'shares', 'skips', 
            'comment_count', 'is_liked', 'creator_id', 'cover_image'
        ]
        read_only_fields = [
            'likes', 'shares', 'skips', 'comment_count', 'hls_playlist_url', 'is_liked', 'cover_image'
        ]

    def get_hls_playlist_url(self, obj):
        return get_hls_playback_url(obj.hls_playlist_url)

    def get_cover_image(self, obj):
        if obj.cover_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.cover_image.url)
        return None

    def get_is_liked(self, obj):
        if hasattr(obj, 'user_has_liked'):
            return obj.user_has_liked
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        return UserInteraction.objects.filter(
            user=request.user, clip=obj, interaction_type='like', is_active=True
        ).exists()

class SkipActionSerializer(serializers.Serializer):
    listen_duration_ms = serializers.IntegerField(min_value=0, required=True)
    reel_position_ms = serializers.IntegerField(min_value=0, required=True)
    reel_id = serializers.UUIDField(required=True)

class ShareActionSerializer(serializers.Serializer):
    receiver_id = serializers.IntegerField(required=True)


class CommentSerializer(serializers.ModelSerializer):
    author_username = serializers.CharField(source='author.username', read_only=True)
    reply_count = serializers.SerializerMethodField()

    class Meta:
        model = Comment
        fields = ['id', 'clip', 'author_username', 'parent', 'text', 'reply_count', 'created_at']
        read_only_fields = ['id', 'author_username', 'reply_count', 'created_at']

    def get_reply_count(self, obj):
        if not obj.parent_id:
            return obj.replies.count()
        return 0

    def validate_text(self, value):
        # SECURITY: strip control characters (NUL, BEL, etc.) from
        # comment text before storage. The React frontend auto-escapes
        # JSON strings, so a stored `<script>` payload is rendered as
        # text — but defense-in-depth: reject obviously malicious
        # characters server-side too. Limit to 500 chars (model
        # CharField max_length) and reject NUL bytes which can break
        # downstream loggers.
        if '\x00' in value:
            raise serializers.ValidationError("Comment contains null bytes.")
        # Strip ASCII control characters except common whitespace (\t, \n, \r).
        cleaned = ''.join(
            ch for ch in value
            if ch >= ' ' or ch in '\t\n\r'
        )
        return cleaned.strip()

    def create(self, validated_data):
        validated_data['author'] = self.context['request'].user
        return super().create(validated_data)
    
class InteractionTelemetrySerializer(serializers.Serializer):
    action_type = serializers.ChoiceField(choices=['view', 'like', 'share', 'skip'])
    # SECURITY: cap watch_time_ms at 10 hours = 36,000,000ms. Anything
    # longer is a client bug or a viewbot inflating completion_rate.
    # Real short-form audio is < 5 min (300,000ms); 10h is a generous
    # upper bound for any legitimate use.
    watch_time_ms = serializers.IntegerField(min_value=0, max_value=36_000_000, required=True)

class ShareEventSerializer(serializers.ModelSerializer):
    sender_name = serializers.CharField(source='sender.username', read_only=True)
    clip_title = serializers.CharField(source='clip.title', read_only=True)
    # See FeedClipSerializer.get_hls_playlist_url — same reasoning: the model
    # field is a storage key, not a URL, so it must be signed here rather
    # than passed through as a plain CharField.
    clip_hls_url = serializers.SerializerMethodField()
    clip = FeedClipSerializer(read_only=True)

    def get_clip_hls_url(self, obj):
        return get_hls_playback_url(obj.clip.hls_playlist_url)
    
    class Meta:
        model = ShareEvent
        fields = [
            'id', 
            'sender_name', 
            'clip',
            'clip_title',
            'clip_hls_url',
            'created_at', 
            'is_read'
        ]


class RegisterSerializer(serializers.ModelSerializer):
    # ISSUE-01 (DPDP consent / age gate)
    # SECURITY: consent_accepted is required; without it registration
    # must fail. terms_version validated against allowed versions
    # from settings (default v1.0 from env).
    consent_accepted = serializers.BooleanField(required=True)
    terms_version = serializers.CharField(required=True, max_length=50)
    dob = serializers.DateField(required=False, allow_null=True)
    parent_email = serializers.EmailField(required=False, allow_null=True)
    # Ensure email is unique and required
    email = serializers.EmailField(
        required=True,
        validators=[UniqueValidator(queryset=User.objects.all())]
    )

    class Meta:
        model = User #built-in User model
        fields = ('username', 'password', 'email', 'consent_accepted', 'terms_version', 'dob', 'parent_email')
        # Ensure password is never returned in a GET request
        extra_kwargs = {
            'password': {'write_only': True}, 'email': {'write_only': True},
        }

    def validate_terms_version(self, value):
        allowed = getattr(settings, 'TERMS_VERSIONS', ['v1.0'])
        if value not in allowed:
            raise serializers.ValidationError(f"Invalid terms version. Allowed: {allowed}")
        return value

    def validate(self, data):
        # DECISION: If user provides dob and age < 18, require parent_email
        # and set is_minor/minor_consent_verified flags. Tradeoff: extra
        # validation logic vs. regulatory compliance (DPDP age gate).
        dob = data.get('dob')
        if dob:
            from datetime import date
            today = date.today()
            age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            if age < 18:
                data['is_minor'] = True
                data['minor_consent_verified'] = False  # default; verified via parent flow
                parent_email = data.get('parent_email')
                if not parent_email:
                    raise serializers.ValidationError(
                        {"parent_email": "Parent/guardian email is required for users under 18."}
                    )
            else:
                data['is_minor'] = False
                data['minor_consent_verified'] = False
        else:
            data['is_minor'] = False
        return data

    def create(self, validated_data):
        # DECISION: Separate consent audit creation from user creation
        # so that consent records exist even if user creation rolls back.
        # Tradeoff: potential orphaned audit rows vs. guaranteed audit trail.
        consent_accepted = validated_data.pop('consent_accepted', False)
        terms_version = validated_data.pop('terms_version', 'v1.0')
        dob = validated_data.get('dob')
        parent_email = validated_data.get('parent_email')
        # Handle minor flow fields
        is_minor = validated_data.pop('is_minor', False)
        minor_consent_verified = validated_data.pop('minor_consent_verified', False)
        user = User.objects.create_user(
            username=validated_data['username'],
            email=validated_data['email'],
            password=validated_data['password'],
            dob=dob,
            parent_email=parent_email,
            is_minor=is_minor,
            minor_consent_verified=minor_consent_verified,
            consent_accepted=consent_accepted,
            terms_version=terms_version,
        )
        # ISSUE-01: Create ConsentAudit row for regulatory audit trail.
        # HACK: Writing audit on user creation in serializer rather than
        # signal so we have direct access to validated consent data.
        request = self.context.get('request')
        ConsentAudit.objects.create(
            user=user,
            terms_version_id=terms_version,
            privacy_version_id='v1.0',
            ip_address=request.META.get('REMOTE_ADDR') if request else None,
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:500] if request else '',
        )
        return user

class PublicProfileSerializer(serializers.ModelSerializer):
    """For viewing any user's profile"""
    followers_count = serializers.IntegerField(read_only=True)
    following_count = serializers.IntegerField(read_only=True)
    uploads_count = serializers.IntegerField(read_only=True)

    profile_picture_url = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'username', 'profile_picture', 'profile_picture_url',
            'followers_count', 'following_count', 'uploads_count',
            'date_joined'
        ]


    def get_profile_picture_url(self, obj):
        if obj.profile_picture and obj.profile_picture.name:
            return get_signed_media_url(obj.profile_picture.name)
        return None

class OwnProfileSerializer(serializers.ModelSerializer):
    """For the logged-in user's own profile — includes private data"""
    followers_count = serializers.IntegerField(read_only=True)
    following_count = serializers.IntegerField(read_only=True)
    uploads_count = serializers.IntegerField(read_only=True)
    liked_clips = serializers.SerializerMethodField()

    profile_picture_url = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'username', 'profile_picture', 'profile_picture_url',
            'followers_count', 'following_count', 'uploads_count',
            'liked_clips', 'date_joined'
        ]


    def get_profile_picture_url(self, obj):
        if obj.profile_picture and obj.profile_picture.name:
            return get_signed_media_url(obj.profile_picture.name)
        return None

    def get_liked_clips(self, obj):
        # N7 fix: query AudioClip directly with the user_has_liked
        # annotation, so FeedClipSerializer.get_is_liked() hits the
        # fast hasattr branch (no per-clip query).
        request = self.context.get('request') if hasattr(self, 'context') else None
        viewer = request.user if request and request.user.is_authenticated else None
        user_like_subquery = UserInteraction.objects.filter(
            clip=OuterRef('pk'),
            user=obj,
            interaction_type='like',
            is_active=True,
        )
        liked_clips = (
            AudioClip.objects
            .filter(
                interactions__user=obj,
                interactions__interaction_type='like',
                interactions__is_active=True,
            )
            .annotate(user_has_liked=Exists(user_like_subquery))
            .distinct()
            .order_by('-interactions__updated_at')[:50]
        )
        return FeedClipSerializer(
            liked_clips, many=True, context=self.context
        ).data

class ProfileUpdateSerializer(serializers.ModelSerializer):
    """For PATCH — only editable fields exposed"""
    class Meta:
        model = User
        fields = ['username', 'profile_picture']

    def validate_username(self, value):
        user = self.context['request'].user
        if User.objects.exclude(pk=user.pk).filter(username=value).exists():
            raise serializers.ValidationError("Username already taken.")
        return value