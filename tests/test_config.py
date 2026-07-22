import pytest

from plane_pages_mcp.config import Config, ConfigError


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in [
        "DATABASE_URL", "SERVICE_USER_ID", "PLANE_BASE_URL", "PLANE_API_KEY",
        "WORKSPACE_SLUG", "MCP_TRANSPORT", "MCP_AUTH_TOKEN", "LIVE_CONVERT_URL",
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")  # avoid the http auth requirement


def test_pages_only(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("SERVICE_USER_ID", "u")
    cfg = Config.from_env()
    assert cfg.pages_enabled and not cfg.rest_enabled


def test_rest_only(monkeypatch):
    monkeypatch.setenv("PLANE_BASE_URL", "http://host")
    monkeypatch.setenv("PLANE_API_KEY", "pat")
    cfg = Config.from_env()
    assert cfg.rest_enabled and not cfg.pages_enabled
    assert cfg.plane_base_url == "http://host"  # trailing slash stripped elsewhere


def test_base_url_trailing_slash_stripped(monkeypatch):
    monkeypatch.setenv("PLANE_BASE_URL", "http://host/")
    monkeypatch.setenv("PLANE_API_KEY", "pat")
    assert Config.from_env().plane_base_url == "http://host"


def test_both(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("SERVICE_USER_ID", "u")
    monkeypatch.setenv("PLANE_BASE_URL", "http://host")
    monkeypatch.setenv("PLANE_API_KEY", "pat")
    cfg = Config.from_env()
    assert cfg.pages_enabled and cfg.rest_enabled


def test_nothing_configured_errors():
    with pytest.raises(ConfigError):
        Config.from_env()


def test_pages_without_service_user_errors(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_http_requires_auth_token(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("PLANE_BASE_URL", "http://host")
    monkeypatch.setenv("PLANE_API_KEY", "pat")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_workspace_slug_is_optional_default(monkeypatch):
    monkeypatch.setenv("PLANE_BASE_URL", "http://host")
    monkeypatch.setenv("PLANE_API_KEY", "pat")
    assert Config.from_env().workspace_slug is None
