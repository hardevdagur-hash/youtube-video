"""Service for communicating with the YouTube Data API for channel operations."""

import logging
from typing import Any

from googleapiclient.errors import HttpError

from api.youtube_client import YouTubeAPIClientError, YouTubeClient

logger = logging.getLogger(__name__)


class ChannelServiceError(Exception):
    """Base exception for channel service API errors."""
    pass


class ChannelNotFoundError(ChannelServiceError):
    """Raised when the API returns no matching channel for a handle."""
    pass


class ChannelService:
    """Handles YouTube Data API communication for channel-related lookups.

    Uses the existing YouTubeClient from Phase 1 for authenticated API access.
    """

    def __init__(self, client: YouTubeClient | None = None) -> None:
        self._client = client or YouTubeClient()

    def resolve_handle(self, handle: str) -> dict[str, Any]:
        """Resolve a channel handle to its channel data via the YouTube API.

        Calls ``channels.list`` with the ``forHandle`` parameter.

        Args:
            handle: The channel handle, with or without ``@`` prefix.

        Returns:
            The first item from the API response containing channel details.

        Raises:
            ChannelNotFoundError: If the handle matches no channel.
            ChannelServiceError: If the API request fails (auth, quota, network).
        """
        service = self._client.get_service()
        clean_handle = handle.lstrip("@")
        logger.info("Calling channels.list with forHandle=%s", clean_handle)

        try:
            response: dict[str, Any] = (
                service.channels()
                .list(part="id,snippet", forHandle=clean_handle)
                .execute()
            )
        except HttpError as exc:
            status = exc.resp.status
            reason = getattr(exc, "reason", str(exc))
            logger.error("YouTube API HTTP %d error: %s", status, reason)

            if status == 403:
                error_body = str(exc)
                if "quotaExceeded" in error_body or "quota" in error_body.lower():
                    raise ChannelServiceError(
                        "API quota exceeded. Try again later or check your quota limits."
                    ) from exc
                raise ChannelServiceError(
                    f"API request forbidden. Check your API key and permissions. Reason: {reason}"
                ) from exc
            if status == 400:
                raise ChannelServiceError(
                    f"Bad API request. Reason: {reason}"
                ) from exc
            raise ChannelServiceError(f"HTTP {status}: {reason}") from exc
        except YouTubeAPIClientError as exc:
            raise ChannelServiceError(str(exc)) from exc
        except Exception as exc:
            logger.error("Unexpected error during API call: %s", exc)
            raise ChannelServiceError(
                f"Unexpected API error: {exc}"
            ) from exc

        items = response.get("items", [])
        if not items:
            raise ChannelNotFoundError(f"No channel found for handle: {handle}")

        logger.info("Channel API response received for handle=%s", handle)
        return items[0]

    def get_channel_by_id(self, channel_id: str) -> dict[str, Any]:
        """Look up a channel by its YouTube channel ID via the API.

        Calls ``channels.list`` with the ``id`` parameter.

        Args:
            channel_id: The YouTube channel ID (``UC...`` format).

        Returns:
            The first item from the API response containing channel details.

        Raises:
            ChannelNotFoundError: If the channel ID matches no channel.
            ChannelServiceError: If the API request fails (auth, quota, network).
        """
        service = self._client.get_service()
        logger.info("Calling channels.list with id=%s", channel_id)

        try:
            response: dict[str, Any] = (
                service.channels()
                .list(part="id,snippet", id=channel_id)
                .execute()
            )
        except HttpError as exc:
            status = exc.resp.status
            reason = getattr(exc, "reason", str(exc))
            logger.error("YouTube API HTTP %d error: %s", status, reason)

            if status == 403:
                error_body = str(exc)
                if "quotaExceeded" in error_body or "quota" in error_body.lower():
                    raise ChannelServiceError(
                        "API quota exceeded. Try again later or check your quota limits."
                    ) from exc
                raise ChannelServiceError(
                    f"API request forbidden. Check your API key and permissions. Reason: {reason}"
                ) from exc
            if status == 400:
                raise ChannelServiceError(
                    f"Bad API request. Reason: {reason}"
                ) from exc
            raise ChannelServiceError(f"HTTP {status}: {reason}") from exc
        except YouTubeAPIClientError as exc:
            raise ChannelServiceError(str(exc)) from exc
        except Exception as exc:
            logger.error("Unexpected error during API call: %s", exc)
            raise ChannelServiceError(
                f"Unexpected API error: {exc}"
            ) from exc

        items = response.get("items", [])
        if not items:
            raise ChannelNotFoundError(f"No channel found for ID: {channel_id}")

        logger.info("Channel API response received for id=%s", channel_id)
        return items[0]
