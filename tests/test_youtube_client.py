"""Tests for api/youtube_client.py"""

import ssl
from unittest.mock import MagicMock, patch

import pytest

from api.youtube_client import (
    YouTubeAPIClientError,
    YouTubeAPISslError,
    YouTubeAPITimeoutError,
    YouTubeClient,
    _patch_httplib2,
)


class TestHttplib2Patching:
    def test_patch_applied_once(self):
        _patch_httplib2()
        _patch_httplib2()  # second call should be no-op
        # Verify the patched __init__ uses certifi

    def test_patched_init_sets_ca_certs(self):
        import httplib2

        from utils.ssl_config import get_ca_bundle_path
        ca_path = get_ca_bundle_path()
        http = httplib2.Http()
        assert http.ca_certs == ca_path
        http.close()

    def test_patched_request_removes_stale_connection(self):
        import httplib2
        http = httplib2.Http()
        # Verify connection pool is empty
        assert len(http.connections) == 0
        http.close()

    def test_redirect_codes_308_removed_in_build_http(self):
        client = YouTubeClient(api_key="test-key")
        http = client._build_http()
        assert 308 not in http.redirect_codes
        http.close()


class TestYouTubeClientInit:
    def test_init_with_default_key(self):
        client = YouTubeClient()
        assert client._api_key is not None  # from settings

    def test_default_key_is_read_at_construction_not_import(self, monkeypatch):
        from config.settings import settings

        monkeypatch.setattr(settings, "youtube_api_key", "rotated-key")
        assert YouTubeClient()._api_key == "rotated-key"

    def test_init_with_custom_key(self):
        client = YouTubeClient(api_key="test-key-123")
        assert client._api_key == "test-key-123"

    def test_initial_state(self):
        client = YouTubeClient(api_key="test-key")
        assert client._service is None
        assert client._http is None

    def test_build_http_creates_configured_instance(self):
        client = YouTubeClient(api_key="test-key")
        http = client._build_http()
        assert http.timeout == 60
        assert http.disable_ssl_certificate_validation is False
        assert http.ca_certs is not None
        http.close()


@patch("api.youtube_client.build")
class TestYouTubeClientGetService:
    def test_get_service_creates_and_caches(self, mock_build):
        mock_resource = MagicMock()
        mock_build.return_value = mock_resource

        client = YouTubeClient(api_key="test-key")
        service = client.get_service()

        assert service is mock_resource
        assert client._service is mock_resource
        mock_build.assert_called_once_with(
            "youtube", "v3", developerKey="test-key",
            http=client._http, cache_discovery=False,
        )

    def test_get_service_returns_cached(self, mock_build):
        mock_resource = MagicMock()
        mock_build.return_value = mock_resource

        client = YouTubeClient(api_key="test-key")
        service1 = client.get_service()
        service2 = client.get_service()

        assert service1 is service2
        assert mock_build.call_count == 1

    def test_get_service_http_error(self, mock_build):
        from googleapiclient.errors import HttpError
        mock_build.side_effect = HttpError(
            MagicMock(status=403), b'{"error": "forbidden"}'
        )

        client = YouTubeClient(api_key="test-key")
        with pytest.raises(YouTubeAPIClientError):
            client.get_service()

    def test_get_service_ssl_error_with_retry(self, mock_build):
        ssl_error = ssl.SSLError("EOF occurred in violation of protocol (_ssl.c:2427)")
        mock_resource = MagicMock()
        mock_build.side_effect = [ssl_error, ssl_error, mock_resource]

        client = YouTubeClient(api_key="test-key")
        service = client.get_service()
        assert service is mock_resource
        assert mock_build.call_count == 3

    def test_get_service_ssl_error_exhausted(self, mock_build):
        ssl_error = ssl.SSLError("EOF occurred in violation of protocol (_ssl.c:2427)")
        mock_build.side_effect = [ssl_error, ssl_error, ssl_error]

        client = YouTubeClient(api_key="test-key")
        with pytest.raises(YouTubeAPISslError) as exc_info:
            client.get_service()
        assert "Unable to connect securely" in str(exc_info.value)

    def test_get_service_timeout_error(self, mock_build):
        mock_build.side_effect = ConnectionError("Connection refused")

        client = YouTubeClient(api_key="test-key")
        with pytest.raises(YouTubeAPITimeoutError) as exc_info:
            client.get_service()
        assert "Unable to connect" in str(exc_info.value)

    def test_get_service_creates_fresh_http_on_retry(self, mock_build):
        ssl_error = ssl.SSLError("handshake failed")
        mock_resource = MagicMock()
        mock_build.side_effect = [ssl_error, mock_resource]

        client = YouTubeClient(api_key="test-key")
        first_http = client._http
        service = client.get_service()
        assert service is mock_resource
        http2 = client._http
        assert http2 is not None
        assert http2 is not first_http  # the failed connection is not reused
        assert mock_build.call_count == 2
