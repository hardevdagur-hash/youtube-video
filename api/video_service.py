"""Service for communicating with YouTube Data API for video/playlist operations."""

import http.client
import logging
import socket
import ssl
import time
from typing import Any

from googleapiclient.errors import HttpError

from api.youtube_client import YouTubeAPIClientError, YouTubeAPISslError, YouTubeClient
from utils.retry import retry

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_SSL_RETRYABLE = (ssl.SSLError, ssl.SSLZeroReturnError, ssl.SSLEOFError,
                  ConnectionError, OSError, socket.timeout,
                  http.client.IncompleteRead)


class VideoServiceError(Exception):
    """Base exception for video service API errors."""
    pass


def _execute_with_ssl_retry(request: Any, max_retries: int = 3) -> dict[str, Any]:
    """Execute a googleapiclient request with SSL/connection retry.

    googleapiclient's ``_retry_request`` already retries SSL errors internally,
    but this adds an additional outer retry loop for resilience.

    Args:
        request: A googleapiclient ``HttpRequest`` object.
        max_retries: Maximum number of outer retries.

    Returns:
        API response dict.

    Raises:
        HttpError: Non-retryable HTTP errors.
        VideoServiceError: On SSL error exhaustion.
    """
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return request.execute()
        except _SSL_RETRYABLE as exc:
            last_error = exc
            if attempt < max_retries:
                delay = 2 ** attempt
                logger.warning(
                    "SSL/Connection error (outer retry %d/%d): %s. Retrying in %ds...",
                    attempt, max_retries, exc, delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "SSL/Connection error exhausted after %d retries: %s",
                    max_retries, exc,
                )
        except HttpError:
            raise  # Let the caller handle HTTP errors
    raise YouTubeAPISslError(
        "Unable to connect securely to the YouTube API. "
        "Please try again in a few moments."
    ) from last_error


class UploadsPlaylistNotFoundError(VideoServiceError):
    """Raised when a channel has no uploads playlist."""
    pass


def _is_retryable(exc: HttpError) -> bool:
    return exc.resp.status in RETRYABLE_STATUSES


def _handle_http_error(exc: HttpError, context: str = "") -> None:
    status = exc.resp.status
    reason = getattr(exc, "reason", str(exc))
    error_body = str(exc)
    prefix = f"{context}: " if context else ""

    logger.error("%sYouTube API HTTP %d error: %s", prefix, status, reason)

    if _is_retryable(exc):
        raise exc

    if status == 403:
        if "quotaExceeded" in error_body or "quota" in error_body.lower():
            raise VideoServiceError(f"{prefix}API quota exceeded. Try again later.") from exc
        raise VideoServiceError(f"{prefix}API request forbidden. Check API key. Reason: {reason}") from exc
    if status == 400:
        raise VideoServiceError(f"{prefix}Bad request. Reason: {reason}") from exc
    if status == 404:
        raise VideoServiceError(f"{prefix}Resource not found: {reason}") from exc
    raise VideoServiceError(f"{prefix}HTTP {status}: {reason}") from exc


class VideoService:
    """Handles YouTube Data API communication for video and playlist lookups.

    Uses retry with exponential backoff for transient failures (429, 5xx).
    """

    def __init__(self, client: YouTubeClient | None = None) -> None:
        self._client = client or YouTubeClient()

    @retry(max_retries=3, base_delay=1.0, exceptions=(HttpError,))
    def get_uploads_playlist_id(self, channel_id: str) -> str:
        service = self._client.get_service()
        logger.info("Fetching uploads playlist ID for channel %s", channel_id)

        try:
            response: dict[str, Any] = _execute_with_ssl_retry(
                service.channels().list(part="contentDetails", id=channel_id),
            )
        except HttpError as exc:
            if _is_retryable(exc):
                raise  # let @retry handle it
            _handle_http_error(exc, "get_uploads_playlist_id")
            raise
        except (YouTubeAPISslError, YouTubeAPIClientError) as exc:
            raise VideoServiceError(str(exc)) from exc
        except Exception as exc:
            logger.error("Unexpected error fetching playlist: %s", exc)
            raise VideoServiceError(f"Unexpected error: {exc}") from exc

        items = response.get("items", [])
        if not items:
            raise VideoServiceError(f"No channel found for ID: {channel_id}")

        uploads_id = (
            items[0]
            .get("contentDetails", {})
            .get("relatedPlaylists", {})
            .get("uploads")
        )
        if not uploads_id:
            raise UploadsPlaylistNotFoundError(
                f"Channel {channel_id} has no uploads playlist."
            )

        logger.info("Uploads playlist ID: %s", uploads_id)
        return uploads_id

    @retry(max_retries=3, base_delay=1.0, exceptions=(HttpError,))
    def get_playlist_items(
        self,
        playlist_id: str,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        service = self._client.get_service()
        page_label = f"page_token={page_token}" if page_token else "first page"
        logger.info("Requesting playlist items: playlist=%s, %s", playlist_id, page_label)

        params: dict[str, Any] = {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            response: dict[str, Any] = _execute_with_ssl_retry(
                service.playlistItems().list(**params),
            )
        except HttpError as exc:
            if _is_retryable(exc):
                raise
            _handle_http_error(exc, "get_playlist_items")
            raise
        except (YouTubeAPISslError, YouTubeAPIClientError) as exc:
            raise VideoServiceError(str(exc)) from exc
        except Exception as exc:
            logger.error("Unexpected error fetching playlist items: %s", exc)
            raise VideoServiceError(f"Unexpected error: {exc}") from exc

        items = response.get("items", [])
        video_ids: list[str] = []
        for item in items:
            resource_id = (
                item.get("snippet", {})
                .get("resourceId", {})
            )
            kind = resource_id.get("kind")
            v_id = resource_id.get("videoId")
            if kind == "youtube#video" and v_id:
                video_ids.append(v_id)

        next_token: str | None = response.get("nextPageToken")
        logger.info(
            "Page received: %d items, nextPageToken=%s",
            len(video_ids),
            next_token or "None",
        )

        return {
            "video_ids": video_ids,
            "next_page_token": next_token,
            "page_item_count": len(video_ids),
        }

    @retry(max_retries=3, base_delay=1.0, exceptions=(HttpError,))
    def get_videos_batch(self, video_ids: list[str]) -> list[dict[str, Any]]:
        if not video_ids:
            return []

        service = self._client.get_service()
        joined = ",".join(video_ids)
        logger.info(
            "Fetching metadata batch: %d videos (ids: %s..)",
            len(video_ids),
            joined[:60],
        )

        try:
            response: dict[str, Any] = _execute_with_ssl_retry(
                service.videos().list(
                    part="snippet,contentDetails,statistics",
                    id=joined,
                ),
            )
        except HttpError as exc:
            if _is_retryable(exc):
                raise
            _handle_http_error(exc, "get_videos_batch")
            raise
        except (YouTubeAPISslError, YouTubeAPIClientError) as exc:
            raise VideoServiceError(str(exc)) from exc
        except Exception as exc:
            logger.error("Unexpected error fetching video batch: %s", exc)
            raise VideoServiceError(f"Unexpected error: {exc}") from exc

        items: list[dict[str, Any]] = response.get("items", [])
        logger.info("Batch response: %d items of %d requested", len(items), len(video_ids))
        return items
