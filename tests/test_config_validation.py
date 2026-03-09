import pytest
from fastapi.testclient import TestClient

from app.core.config import (
    DEFAULT_CALENDLY_WEBHOOK_SIGNING_KEY,
    DEFAULT_JWT_SECRET,
    DEFAULT_STRIPE_CONNECT_AUTHORIZE_URL,
    DEFAULT_STRIPE_CONNECT_CLIENT_ID,
    DEFAULT_STRIPE_CONNECT_REDIRECT_URI,
    DEFAULT_STRIPE_WEBHOOK_SECRET,
    DEFAULT_TRACKED_LINK_BASE_URL,
    Settings,
    SettingsValidationError,
    get_settings,
)
from app.main import app

SAFE_NON_LOCAL_ENV = {
    "APP_ENV": "preview",
    "JWT_SECRET": "story55-preview-jwt-secret-0123456789abcdef",
    "STRIPE_CONNECT_CLIENT_ID": "ca_story55_preview_live",
    "STRIPE_CONNECT_AUTHORIZE_URL": DEFAULT_STRIPE_CONNECT_AUTHORIZE_URL,
    "STRIPE_CONNECT_REDIRECT_URI": "https://creatortrust.test/stripe/connect/callback",
    "STRIPE_WEBHOOK_SECRET": "whsec_story55_preview_live",
    "CALENDLY_WEBHOOK_SIGNING_KEY": "cal_story55_preview_live",
    "TRACKED_LINK_BASE_URL": "https://trk.creatortrust.test",
}


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _safe_non_local_settings(**overrides: str) -> Settings:
    data = {
        "app_env": "preview",
        "jwt_secret": SAFE_NON_LOCAL_ENV["JWT_SECRET"],
        "stripe_connect_client_id": SAFE_NON_LOCAL_ENV["STRIPE_CONNECT_CLIENT_ID"],
        "stripe_connect_authorize_url": SAFE_NON_LOCAL_ENV["STRIPE_CONNECT_AUTHORIZE_URL"],
        "stripe_connect_redirect_uri": SAFE_NON_LOCAL_ENV["STRIPE_CONNECT_REDIRECT_URI"],
        "stripe_webhook_secret": SAFE_NON_LOCAL_ENV["STRIPE_WEBHOOK_SECRET"],
        "calendly_webhook_signing_key": SAFE_NON_LOCAL_ENV["CALENDLY_WEBHOOK_SIGNING_KEY"],
        "tracked_link_base_url": SAFE_NON_LOCAL_ENV["TRACKED_LINK_BASE_URL"],
    }
    data.update(overrides)
    return Settings.model_validate(data)


def _set_non_local_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env_values = dict(SAFE_NON_LOCAL_ENV)
    env_values.update(overrides)
    for name, value in env_values.items():
        monkeypatch.setenv(name, value)


def test_local_defaults_pass_runtime_validation():
    settings = Settings.model_validate({"app_env": "local"})

    settings.validate_runtime()


def test_non_local_defaults_fail_with_clear_field_names():
    settings = Settings.model_validate({"app_env": "preview"})

    with pytest.raises(SettingsValidationError) as exc_info:
        settings.validate_runtime()

    message = str(exc_info.value)
    assert "jwt_secret" in message
    assert "stripe_webhook_secret" in message
    assert "calendly_webhook_signing_key" in message
    assert "stripe_connect_client_id" in message
    assert "stripe_connect_redirect_uri" in message
    assert "tracked_link_base_url" in message
    assert DEFAULT_JWT_SECRET not in message
    assert DEFAULT_STRIPE_WEBHOOK_SECRET not in message
    assert DEFAULT_CALENDLY_WEBHOOK_SIGNING_KEY not in message
    assert DEFAULT_STRIPE_CONNECT_CLIENT_ID not in message
    assert DEFAULT_STRIPE_CONNECT_REDIRECT_URI not in message
    assert DEFAULT_TRACKED_LINK_BASE_URL not in message


def test_non_local_safe_settings_pass_runtime_validation():
    settings = _safe_non_local_settings()

    settings.validate_runtime()


def test_app_startup_fails_fast_for_non_local_placeholder_jwt_secret(monkeypatch: pytest.MonkeyPatch):
    _set_non_local_env(monkeypatch, JWT_SECRET=DEFAULT_JWT_SECRET)

    with pytest.raises(SettingsValidationError, match="jwt_secret"):
        with TestClient(app):
            pass


def test_app_startup_succeeds_for_non_local_safe_settings(monkeypatch: pytest.MonkeyPatch):
    _set_non_local_env(monkeypatch)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
