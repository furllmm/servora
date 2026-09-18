import pytest
from servora.server_browser import BareServerBrowser, validate_server_url


def test_url():
    assert validate_server_url("http://127.0.0.1:8080")


def test_bad_url():
    with pytest.raises(ValueError):
        validate_server_url("file:///etc/passwd")


def test_credentials_are_rejected():
    with pytest.raises(ValueError):
        validate_server_url("http://user:pass@127.0.0.1:8080")


def test_bare_browser_without_renderer():
    result = BareServerBrowser().open("http://127.0.0.1:8080")
    assert result["mode"] == "bare"
    assert result["status"] == "renderer_unavailable"
