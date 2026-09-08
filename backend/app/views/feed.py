"""Feed, suggestion, and tag-init views.

DECISION: Split out of monolithic views.py in 2026-09. Each of these
touches the recommendation engine / Redis feed cache, so they share
a single module. ~270 lines.
"""
import logging
import numpy as np
from django.core.cache import cache
from django.db.models import Exists, OuterRef, Case, When
from pgvector.django import CosineDistance
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response

from ..models import AudioClip, UserInteraction
from ..serializers import FeedClipSerializer
from ..services.interactions import invalidate_user_vectors_cache
from ..tasks import refill_user_feed, calculate_time_decayed_vectors
from ..services.task_publisher import publish
from ._pagination import FeedCursorPagination


# N11 fix: cache the user's blended vector in Redis. Without this,
# /suggestions/?category=X runs calculate_time_decayed_vectors inline
# on every request, hitting Postgres for the last 50 interactions and
# doing numpy math per request. With the cache, a single computation
# is reused across 15 min, invalidated when the user takes a new action.
# Trade-off: 15-min staleness on explore recommendations; FastFeed (the
# main feed) is unaffected (it reads pre-computed vectors from Redis
# via refill_user_feed, not via this helper).
_USER_VECTORS_TTL_SECONDS = 900  # 15 min
_USER_VECTORS_KEY = 'user_vectors:{user_id}'


def get_user_vectors(user):
    """Return (semantic_vec, acoustic_vec) for a user, with Redis cache.

    Returns (None, None) on cache miss + no interactions (cold start).
    Cache key: 'user_vectors:{user_id}'. TTL: 15 min.
    """
    cache_key = _USER_VECTORS_KEY.format(user_id=user.id)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    sem, ac = calculate_time_decayed_vectors(user)
    if sem is not None and ac is not None:
        cache.set(cache_key, (sem, ac), timeout=_USER_VECTORS_TTL_SECONDS)
    return sem, ac


# invalidate_user_vectors_cache is imported from
# backend.app.services.interactions at the top of this file. The
# helper is a single source of truth there; the import in this module
# is for backwards-compat with anything that imports the name from
# views/feed (the audit doc references this path; the test
# test_adversarial_pass3.py:472 checks hasattr here).


class FastFeedViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request):
        user_id = request.user.id
        redis_key = f"user_feed:{user_id}"

        # DECISION: Wrap the entire Redis path in try/except. If Redis is
        # unreachable, return a trending-feed fallback (top clips by
        # engagement_velocity) instead of 500ing. The architecture audit
        # warns that a Redis outage during a 5k-user peak would otherwise
        # firehose the database with 5k concurrent refill_user_feed tasks
        # and crash PostgreSQL.
        try:
            redis_client = cache.client.get_client()
            clip_ids_bytes = redis_client.lpop(redis_key, 10)

            if not clip_ids_bytes:
                publish(refill_user_feed, user_id, count=40)
                # N6 fix: refill_user_feed.delay() is async. The second
                # lpop immediately after runs in the same request thread,
                # *before* the worker has executed the refill. On a cold
                # queue (new user, expired 24h TTL, broker hiccup) this
                # second lpop almost always returns None, so we used to
                # return "You've caught up!" — telling the user the feed
                # is empty when it's actually about to be populated. The
                # fix is to return 202 Accepted with a retry_after_ms
                # hint so the client can poll again in ~1.5s and find
                # the freshly-populated queue.
                clip_ids_bytes = redis_client.lpop(redis_key, 10)

                if not clip_ids_bytes:
                    return Response(
                        {
                            "results": [],
                            "message": "Preparing your feed...",
                            "retry_after_ms": 1500,
                            "degraded": True,
                        },
                        status=status.HTTP_202_ACCEPTED,
                    )

            clip_ids = [vid.decode('utf-8') for vid in clip_ids_bytes]
            queue_length = redis_client.llen(redis_key)

            preserved_order = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(clip_ids)])
            user_like_subquery = UserInteraction.objects.filter(
                clip=OuterRef('pk'), user=request.user, interaction_type='like'
            )
            clips = (
                AudioClip.objects
                .filter(id__in=clip_ids, moderation_approved=True)
                # SECURITY: Exclude NC + SA items from user feeds. NC items
                # are filtered at runtime via SCRAPER_ALLOW_NC; SA items are
                # operator-gated via /clips/{id}/approve-moderation/.
                .filter(is_noncommercial=False, requires_share_alike=False)
                .annotate(user_has_liked=Exists(user_like_subquery))
                .order_by(preserved_order)
            )
            serializer = FeedClipSerializer(clips, many=True, context={'request': request})
            return Response({
                "next": "auto_trigger",
                "queue_health": queue_length,
                "results": serializer.data,
            })
        except Exception as e:
            logging.getLogger(__name__).warning(
                "feed service degraded for user %s; serving trending fallback: %s",
                user_id, e,
            )
            fallback = (
                AudioClip.objects
                .filter(status='ready', moderation_approved=True)
                # SECURITY: Same NC + SA exclusion as primary feed path.
                .filter(is_noncommercial=False, requires_share_alike=False)
                .annotate(user_has_liked=Exists(
                    UserInteraction.objects.filter(
                        clip=OuterRef('pk'), user=request.user, interaction_type='like'
                    )
                ))
                .order_by('-engagement_velocity', '-created_at')[:20]
            )
            serializer = FeedClipSerializer(fallback, many=True, context={'request': request})
            return Response({
                "next": "auto_trigger",
                "queue_health": 0,
                "degraded": True,
                "results": serializer.data,
            })


class SuggestionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Category-specific recommendations using user's blended preference vectors.

    ENDPOINT: GET /suggestions/explore/?category=comedy
    """
    serializer_class = FeedClipSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = FeedCursorPagination

    def get_queryset(self):
        user = self.request.user
        category = self.request.query_params.get('category') or 'all'

        queryset = AudioClip.objects.filter(
            status='ready', category=category, moderation_approved=True,
            # SECURITY: Same NC + SA exclusion as feed endpoints.
            is_noncommercial=False, requires_share_alike=False,
        )

        # DECISION: Wrap the vector search in try/except. The architecture
        # audit warns that a Postgres/Redis hiccup in
        # calculate_time_decayed_vectors would 500 the whole explore page.
        # With this fallback: rank by combined distance, or on failure
        # rank by engagement_velocity (trending within category), or as
        # a last resort serve the category unranked.
        # N11: get_user_vectors() now caches in Redis for 15 min, so
        # /suggestions/ doesn't recompute on every request.
        # SEC: sanitize the category to keep Prometheus label cardinality bounded.
        # Free-form category strings would explode the metric; we cap at 32 chars
        # and replace anything that isn't a-z/0-9/_/- with '_'.
        import re
        safe_category = re.sub(r'[^a-z0-9_\-]', '_', (category or 'all')[:32]) or 'all'

        from .. import metrics
        with metrics.time_suggestion_ranking(category=safe_category) as timer:
            try:
                sem_query, ac_query = get_user_vectors(user)
                if sem_query and ac_query:
                    queryset = queryset.annotate(
                        combined_distance=(
                            CosineDistance('semantic_vector', sem_query) +
                            CosineDistance('acoustic_vector', ac_query)
                        )
                    ).order_by('combined_distance')
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "vector ranking failed for user %s; falling back to engagement_velocity: %s",
                    user.id, e,
                )
                timer.set_outcome('fallback')
                queryset = queryset.order_by('-engagement_velocity', '-created_at')

        user_like_subquery = UserInteraction.objects.filter(
            clip=OuterRef('pk'), user=user, interaction_type='like'
        )
        return queryset.annotate(user_has_liked=Exists(user_like_subquery))


class TagsViewSet(viewsets.ViewSet):
    """
    Cold-start onboarding: Initialize user preferences from tag selection.

    ENDPOINT: POST /tags/initialize/
    """
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=False, methods=['post'], url_path='initialize')
    def initialize_vectors(self, request):
        user = request.user
        selected_tags = request.data.get('selected_tags', [])

        # N bug fix: tags is a JSONField(default=list) (see models.py:69),
        # NOT a Postgres ArrayField. The old code used Django's ArrayField
        # 'overlap' lookup on a JSONField, which Django silently
        # reinterpreted as a JSON path: `JSON_EXTRACT(tags, '$.overlap')`.
        # That returned 0 rows for every call (no JSON document has a key
        # literally named 'overlap'), so the entire tag-based cold-start
        # UX was silently non-functional.
        #
        # Postgres JSONB has no native "any-of" operator. The right
        # primitive is `@>` (contains): tags @> '["tag"]'::jsonb is true
        # when the array contains "tag". We OR one Q per selected tag.
        # Empty selected_tags short-circuits to no match.
        from django.db.models import Q
        if not selected_tags:
            baseline_clips = AudioClip.objects.none()
        else:
            tag_filter = Q()
            for tag in selected_tags:
                tag_filter |= Q(tags__contains=[tag])
            baseline_clips = AudioClip.objects.filter(
                tag_filter,
                semantic_vector__isnull=False,
                acoustic_vector__isnull=False,
                moderation_approved=True,
            ).order_by('-likes')[:100]

        if not baseline_clips:
            return Response({"error": "Not enough data to build baseline."}, status=400)

        sem_vectors = [np.array(clip.semantic_vector) for clip in baseline_clips]
        ac_vectors = [np.array(clip.acoustic_vector) for clip in baseline_clips]

        user.long_term_semantic = (np.mean(sem_vectors, axis=0)).tolist()
        user.long_term_acoustic = (np.mean(ac_vectors, axis=0)).tolist()
        user.save()

        publish(refill_user_feed, user.id, count=30)

        return Response({"status": "Algorithm initialized. Feed is ready."}, status=200)
