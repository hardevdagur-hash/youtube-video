"""YouTube Data API v3 client.

Uses googleapiclient with a patched httplib2 that:
  - Uses certifi CA bundle by default
  - Removes stale connections from pool on SSL errors
  - Has proper timeouts and logging
"""

import contextlib
import logging
import ssl
import time

import httplib2
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from config.settings import settings
from utils.ssl_config import get_ca_bundle_path

logger = logging.getLogger(__name__)


class YouTubeAPIClientError(Exception):
    """Base exception for YouTube API Client errors."""


class YouTubeAPISslError(YouTubeAPIClientError):
    """SSL/TLS handshake failure when connecting to YouTube API."""


class YouTubeAPITimeoutError(YouTubeAPIClientError):
    """Connection timed out when connecting to YouTube API."""


# ---------------------------------------------------------------------------
# httplib2 patching — applied once at module import
# ---------------------------------------------------------------------------

_HTTPLIB2_PATCHED = False


def _patch_httplib2() -> None:
    """Patch httplib2.Http to:
    1. Use certifi CA bundle when ``ca_certs`` is not provided
    2. Remove stale connections from the pool on SSL/connection errors
    3. Log every request attempt
    """
    global _HTTPLIB2_PATCHED
    if _HTTPLIB2_PATCHED:
        return
    _HTTPLIB2_PATCHED = True

    original_request = httplib2.Http.request
    original_init = httplib2.Http.__init__

    def _patched_init(self, cache=None, timeout=None, proxy_info=None,
                      ca_certs=None, disable_ssl_certificate_validation=False,
                      tls_maximum_version=None, tls_minimum_version=None,
                      **kwargs):
        if ca_certs is None and not disable_ssl_certificate_validation:
            ca_certs = get_ca_bundle_path()
            logger.debug("httplib2.Http using certifi CA bundle")
        original_init(
            self, cache=cache, timeout=timeout, proxy_info=proxy_info,
            ca_certs=ca_certs,
            disable_ssl_certificate_validation=disable_ssl_certificate_validation,
            tls_maximum_version=tls_maximum_version,
            tls_minimum_version=tls_minimum_version,
            **kwargs,
        )

    def _patched_request(self, uri, method="GET", body=None, headers=None,
                         redirections=httplib2.DEFAULT_MAX_REDIRECTS,
                         connection_type=None):
        # Ensure ca_certs is set
        if self.ca_certs is None and not self.disable_ssl_certificate_validation:
            self.ca_certs = get_ca_bundle_path()

        conn_key = None
        try:
            from urllib.parse import urlparse
            parsed = urlparse(uri)
            scheme = parsed.scheme
            authority = parsed.netloc
            conn_key = (scheme, authority, connection_type)

            logger.debug("httplib2 >>> %s %s  conn_pool_size=%d",
                         method.upper(), uri, len(self.connections))

            return original_request(
                self, uri, method=method, body=body, headers=headers,
                redirections=redirections, connection_type=connection_type,
            )

        except (ssl.SSLError, ssl.SSLZeroReturnError, ssl.SSLEOFError,
                ConnectionError, OSError) as exc:
            if conn_key and conn_key in self.connections:
                stale = self.connections.pop(conn_key, None)
                logger.debug(
                    "Removed stale httplib2 connection %s after error: %s",
                    id(stale), exc,
                )
            raise

    httplib2.Http.__init__ = _patched_init
    httplib2.Http.request = _patched_request
    logger.info("httplib2 patched: certifi CA bundle + stale connection cleanup")


_patch_httplib2()


# ---------------------------------------------------------------------------
# YouTubeClient
# ---------------------------------------------------------------------------

class YouTubeClient:
    """Authenticated client for interacting with the YouTube Data API v3.

    Uses a patched httplib2 with:
      - certifi CA bundle for SSL verification
      - Automatic cleanup of stale connections on SSL errors
      - Retry logic with exponential backoff
      - Comprehensive logging before/after each request
    """

    def __init__(self, api_key: str = settings.youtube_api_key) -> None:
        self._api_key = api_key
        self._service: Resource | None = None
        self._http: httplib2.Http | None = None

    def _build_http(self) -> httplib2.Http:
        """Build an httplib2.Http with proper SSL configuration."""
        http = httplib2.Http(
            timeout=60,
            ca_certs=get_ca_bundle_path(),
            disable_ssl_certificate_validation=False,
        )
        with contextlib.suppress(AttributeError):
            http.redirect_codes = http.redirect_codes - {308}
        return http

    def get_service(self) -> Resource:
        """Build and return the YouTube Data API v3 service resource.

        Uses lazy-loading with retry on SSL / connection failures.

        Returns:
            ``googleapiclient.discovery.Resource``

        Raises:
            YouTubeAPISslError: All SSL retries exhausted.
            YouTubeAPITimeoutError: Connection timeout.
            YouTubeAPIClientError: Other client errors.
        """
        if self._service is not None:
            return self._service

        self._http = self._build_http()

        max_retries = 3
        last_error: Exception | None = None

        for attempt in range(1, max_retries + 1):
            try:
                logger.info(
                    "Initializing YouTube Data API client (attempt %d/%d)...",
                    attempt, max_retries,
                )
                if attempt > 1:
                    self._http = self._build_http()

                self._service = build(
                    "youtube",
                    "v3",
                    developerKey=self._api_key,
                    http=self._http,
                    cache_discovery=False,
                )
                logger.info("YouTube Data API client ready.")
                return self._service

            except HttpError as e:
                logger.error("HTTP error creating YouTube client: %s", e)
                raise YouTubeAPIClientError(
                    f"HTTP error during client initialization: {e}"
                ) from e

            except (TimeoutError, ssl.SSLError, ssl.SSLZeroReturnError, ssl.SSLEOFError, ConnectionError, OSError) as e:
                last_error = e
                logger.warning(
                    "SSL/Connection error (attempt %d/%d): %s",
                    attempt, max_retries, e,
                )
                if attempt < max_retries:
                    delay = 2 ** attempt
                    logger.info("Retrying in %ds...", delay)
                    time.sleep(delay)

            except Exception as e:
                logger.error("Unexpected error creating YouTube client: %s", e)
                raise YouTubeAPIClientError(
                    f"Failed to setup YouTube client: {e}"
                ) from e

        # All retries exhausted
        if isinstance(last_error, ssl.SSLError | ssl.SSLZeroReturnError | ssl.SSLEOFError):
            raise YouTubeAPISslError(
                "Unable to connect securely to the YouTube API service. "
                "Please try again in a few moments. Technical details have been logged."
            ) from last_error
        raise YouTubeAPITimeoutError(
            "Unable to connect to the YouTube API service. "
            "Please check your network connection."
        ) from last_error
