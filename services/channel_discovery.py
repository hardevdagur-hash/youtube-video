"""Channel upload discovery shared by channel jobs, synchronous channel transcripts and
channel CSV export.

Walks the channel's uploads playlist (newest first), fetches metadata for each page and
applies the eligibility rules *while paging*: the duration window
(CHANNEL_MIN_VIDEO_SECONDS <= duration < CHANNEL_MAX_VIDEO_SECONDS), no live or upcoming
broadcasts, and an optional publish-date range. ``limit`` therefore counts videos that
will actually be processed; previously it capped the uploads *looked at*, so a channel
whose newest uploads were Shorts returned nothing.

Paging stops when ``limit`` eligible videos are found, the playlist ends, uploads older
than ``published_after`` are reached, or ``scan_cap`` uploads were examined (bounds
YouTube quota: each 50-video page costs about 2 units).

The function is blocking (YouTube Data API calls); async callers run it in a thread.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from services.duration_filter import evaluate_duration, parse_iso_duration

logger = logging.getLogger(__name__)

SKIP_OUT_OF_DATE_RANGE = "OUT_OF_DATE_RANGE"
SKIP_NO_METADATA = "NO_METADATA"


@dataclass(frozen=True)
class DiscoveredVideo:
    video_id: str
    title: str
    published_at: str
    duration_seconds: int
    duration_formatted: str
    live_status: str
    skip_reason: str | None  # None = eligible; else TOO_SHORT, TOO_LONG, LIVE_STREAM, ...

    @property
    def eligible(self) -> bool:
        return self.skip_reason is None


@dataclass
class ChannelScan:
    """Every upload examined (newest first) and the eligible ones among them (<= ``limit``)."""

    scanned: list[DiscoveredVideo] = field(default_factory=list)
    eligible: list[DiscoveredVideo] = field(default_factory=list)
    hit_scan_cap: bool = False
    stopped: bool = False  # ``should_stop`` asked to abort (e.g. job cancelled)

    @property
    def skipped(self) -> list[DiscoveredVideo]:
        return [v for v in self.scanned if not v.eligible]

    def skip_counts(self) -> Counter[str]:
        """Skipped uploads by reason (TOO_SHORT, TOO_LONG, LIVE_STREAM, ...)."""
        return Counter(v.skip_reason for v in self.skipped)


def _parse_published(raw: str) -> datetime | None:
    """YouTube ``publishedAt`` as an aware UTC datetime (date boundaries are aware too)."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _classify(
    video_id: str,
    item: dict[str, Any] | None,
    *,
    min_seconds: int,
    max_seconds: int,
    published_after: datetime | None,
    published_before: datetime | None,
) -> DiscoveredVideo:
    if not item:
        return DiscoveredVideo(video_id, "", "", 0, "0:00", "none", SKIP_NO_METADATA)
    snippet = item.get("snippet", {}) or {}
    details = item.get("contentDetails", {}) or {}
    published_at = snippet.get("publishedAt", "") or ""
    live_status = snippet.get("liveBroadcastContent", "none") or "none"
    result = evaluate_duration(
        duration_seconds=parse_iso_duration(details.get("duration", "PT0S")),
        live_status=live_status,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
    )
    reason = result.skip_reason
    published = _parse_published(published_at)
    if reason is None and published is not None and (
        (published_after is not None and published < published_after)
        or (published_before is not None and published > published_before)
    ):
        reason = SKIP_OUT_OF_DATE_RANGE
    return DiscoveredVideo(
        video_id=video_id,
        title=snippet.get("title", "") or "",
        published_at=published_at,
        duration_seconds=result.duration_seconds,
        duration_formatted=result.duration_formatted,
        live_status=live_status,
        skip_reason=reason,
    )


def scan_channel_uploads(
    video_svc: Any,
    playlist_id: str,
    limit: int,
    *,
    min_seconds: int,
    max_seconds: int,
    scan_cap: int,
    published_after: datetime | None = None,
    published_before: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ChannelScan:
    """Find up to ``limit`` eligible uploads (newest first); see the module docstring.

    ``video_svc`` provides ``get_playlist_items(playlist_id, page_token)`` returning
    ``{"video_ids": [...], "next_page_token": str | None}`` and ``get_videos_batch(ids)``
    returning YouTube ``videos.list`` items. A failed metadata batch marks that page's
    videos NO_METADATA (logged) instead of failing the whole discovery.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    scan_cap = max(scan_cap, limit)
    scan = ChannelScan()
    seen: set[str] = set()
    page_token: str | None = None

    while len(scan.eligible) < limit:
        if should_stop is not None and should_stop():
            scan.stopped = True
            break
        page = video_svc.get_playlist_items(playlist_id, page_token)
        new_ids = [v for v in page.get("video_ids", []) if v and v not in seen]
        if not new_ids:
            break
        new_ids = new_ids[: scan_cap - len(scan.scanned)]
        seen.update(new_ids)

        metadata: dict[str, dict[str, Any]] = {}
        try:
            for item in video_svc.get_videos_batch(new_ids):
                if item.get("id"):
                    metadata[item["id"]] = item
        except Exception as exc:
            logger.warning("Metadata batch of %d videos failed; marking them NO_METADATA: %s", len(new_ids), exc)

        reached_cutoff = False
        for video_id in new_ids:
            video = _classify(
                video_id, metadata.get(video_id),
                min_seconds=min_seconds, max_seconds=max_seconds,
                published_after=published_after, published_before=published_before,
            )
            scan.scanned.append(video)
            if video.eligible:
                scan.eligible.append(video)
                if len(scan.eligible) >= limit:
                    break  # uploads after this one were never examined
            published = _parse_published(video.published_at)
            if published_after is not None and published is not None and published < published_after:
                reached_cutoff = True  # uploads are newest first: everything after is older

        if reached_cutoff:
            break
        if len(scan.scanned) >= scan_cap:
            scan.hit_scan_cap = len(scan.eligible) < limit
            break
        page_token = page.get("next_page_token")
        if not page_token:
            break

    logger.info(
        "Channel discovery: scanned %d upload(s), %d eligible (limit %d, window %d-%ds)%s; skipped %s",
        len(scan.scanned), len(scan.eligible), limit, min_seconds, max_seconds,
        " [scan cap reached]" if scan.hit_scan_cap else "", dict(scan.skip_counts()) or "none",
    )
    return scan
