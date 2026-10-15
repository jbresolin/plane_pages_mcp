import pytest

from plane_pages_mcp.config import Config, ConfigError


OAUTH_VARS = {
    "GITHUB_CLIENT_ID": "cid",
    "GITHUB_CLIENT_SECRET": "csecret",
    "PUBLIC_BASE_URL": "https://planemcp.example.com",
    "ALLOWED_GITHUB_LOGINS": "alice,bob",
    "JWT_SIGNING_KEY": "a-long-enough-signing-key",
    "STORAGE_ENCRYPTION_KEY": "a-long-storage-encryption-key",
    "REDIS_URL": "redis://localhost:6379/0",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in [
        "DATABASE_URL", "SERVICE_USER_ID", "PLANE_BASE_URL", "PLANE_API_KEY",
        "WORKSPACE_SLUG", "MCP_TRANSPORT", "MCP_AUTH_TOKEN", "LIVE_CONVERT_URL",
        *OAUTH_VARS,
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")  # avoid the http OAuth requirement


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


def test_workspace_slug_is_optional_default(monkeypatch):
    monkeypatch.setenv("PLANE_BASE_URL", "http://host")
    monkeypatch.setenv("PLANE_API_KEY", "pat")
    assert Config.from_env().workspace_slug is None


# --- http OAuth fail-fast ------------------------------------------------


def _http_env(monkeypatch, **overrides):
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("PLANE_BASE_URL", "http://host")
    monkeypatch.setenv("PLANE_API_KEY", "pat")
    env = {**OAUTH_VARS, **overrides}
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


def test_http_with_full_oauth_config_ok(monkeypatch):
    _http_env(monkeypatch)
    cfg = Config.from_env()
    assert cfg.auth_enabled
    assert cfg.allowed_github_logins == frozenset({"alice", "bob"})
    assert cfg.token_expiry_seconds == 604800


@pytest.mark.parametrize("missing", sorted(OAUTH_VARS))
def test_http_fails_fast_on_any_missing_oauth_var(monkeypatch, missing):
    _http_env(monkeypatch, **{missing: None})
    with pytest.raises(ConfigError) as exc:
        Config.from_env()
    assert missing in str(exc.value)


def test_http_empty_allowlist_fails(monkeypatch):
    _http_env(monkeypatch, ALLOWED_GITHUB_LOGINS="   ,  ,")
    with pytest.raises(ConfigError) as exc:
        Config.from_env()
    assert "ALLOWED_GITHUB_LOGINS" in str(exc.value)


def test_http_short_jwt_key_fails(monkeypatch):
    _http_env(monkeypatch, JWT_SIGNING_KEY="short")
    with pytest.raises(ConfigError) as exc:
        Config.from_env()
    assert "JWT_SIGNING_KEY" in str(exc.value)


def test_stdio_needs_no_oauth(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("PLANE_BASE_URL", "http://host")
    monkeypatch.setenv("PLANE_API_KEY", "pat")
    cfg = Config.from_env()  # must not raise
    assert not cfg.auth_enabled


def test_live_redis_url_defaults_to_redis_url(monkeypatch):
    # Checks: without PLANE_LIVE_REDIS_URL, the "is this page open?" check
    # uses the same Redis as the OAuth storage (REDIS_URL).
    # Why: in the standard deployment both are Plane's own Redis, and Redis
    # pub/sub ignores the database number, so existing deployments get the
    # open-page protection without any new setting.
    monkeypatch.delenv("PLANE_LIVE_REDIS_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("SERVICE_USER_ID", "u")
    monkeypatch.setenv("REDIS_URL", "redis://plane-redis:6379/1")
    assert Config.from_env().live_redis_url == "redis://plane-redis:6379/1"


def test_live_redis_url_can_be_set_separately(monkeypatch):
    # Checks: PLANE_LIVE_REDIS_URL wins over REDIS_URL when both are set.
    # Why: lets an operator keep OAuth state on a different Redis from the
    # one Plane's live server uses.
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("SERVICE_USER_ID", "u")
    monkeypatch.setenv("REDIS_URL", "redis://oauth:6379/1")
    monkeypatch.setenv("PLANE_LIVE_REDIS_URL", "redis://plane-redis:6379/0")
    assert Config.from_env().live_redis_url == "redis://plane-redis:6379/0"


def test_live_redis_url_is_none_without_any_redis(monkeypatch):
    # Checks: with neither variable set (e.g. stdio on a dev box), the value
    # is None, which tells the server to skip the open-page check.
    # Why: Redis must stay optional; the server logs a warning instead.
    monkeypatch.delenv("PLANE_LIVE_REDIS_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("SERVICE_USER_ID", "u")
    assert Config.from_env().live_redis_url is None


def test_default_project_lead_is_read_from_the_environment(monkeypatch):
    # Checks: DEFAULT_PROJECT_LEAD is read (spaces trimmed), and is None when
    # unset or blank.
    # Why: create_project falls back to it when no lead is given. A blank
    # value must mean "no default", not a lookup of an empty name.
    monkeypatch.setenv("PLANE_BASE_URL", "http://host")
    monkeypatch.setenv("PLANE_API_KEY", "pat")
    monkeypatch.setenv("DEFAULT_PROJECT_LEAD", " me@example.com ")
    assert Config.from_env().default_project_lead == "me@example.com"
    monkeypatch.setenv("DEFAULT_PROJECT_LEAD", "  ")
    assert Config.from_env().default_project_lead is None
    monkeypatch.delenv("DEFAULT_PROJECT_LEAD")
    assert Config.from_env().default_project_lead is None
