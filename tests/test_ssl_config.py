"""Tests for utils/ssl_config.py"""

import ssl

import certifi

from utils.ssl_config import create_ssl_context, get_ca_bundle_path, patch_httplib2_ca_certs


class TestCreateSslContext:
    def test_default_context(self):
        ctx = create_ssl_context()
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True

    def test_tlsv1_2_minimum(self):
        ctx = create_ssl_context(tls_version="TLSv1.2")
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2

    def test_tlsv1_3_minimum(self):
        ctx = create_ssl_context(tls_version="TLSv1.3")
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3

    def test_verify_mode_none(self):
        ctx = create_ssl_context(verify_mode=ssl.CERT_NONE)
        assert ctx.verify_mode == ssl.CERT_NONE
        assert ctx.check_hostname is False

    def test_uses_certifi(self):
        ctx = create_ssl_context()
        cafile = ctx.get_ca_certs()
        assert len(cafile) > 0


class TestGetCaBundlePath:
    def test_returns_certifi_path(self):
        path = get_ca_bundle_path()
        assert path == certifi.where()
        assert path.endswith(".pem") or path.endswith(".crt")

    def test_file_exists(self):
        path = get_ca_bundle_path()
        import os
        assert os.path.exists(path)


class TestPatchHttplib2:
    def test_patches_httplib2(self):
        import httplib2

        original_init = httplib2.Http.__init__
        patch_httplib2_ca_certs()
        patched_init = httplib2.Http.__init__
        assert patched_init is not original_init

    def test_patched_init_sets_ca_certs(self):
        import httplib2

        patch_httplib2_ca_certs()
        http = httplib2.Http()
        assert http.ca_certs == certifi.where()
        http.close()

    def test_patched_init_respects_explicit_ca(self):
        import os
        import tempfile

        import httplib2

        patch_httplib2_ca_certs()
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
            f.write(b"test")
            tmp_path = f.name
        try:
            http = httplib2.Http(ca_certs=tmp_path)
            assert http.ca_certs == tmp_path
            http.close()
        finally:
            os.unlink(tmp_path)

    def test_patched_init_respects_disable_ssl(self):
        import httplib2

        patch_httplib2_ca_certs()
        http = httplib2.Http(disable_ssl_certificate_validation=True)
        assert http.ca_certs is None
        http.close()
