import pytest
from fastapi.testclient import TestClient

from app.core.config import (
    DEFAULT_CALENDLY_WEBHOOK_SIGNING_KEY,
    DEFAULT_FULLSCOPE_WEBHOOK_SHARED_SECRET,
    DEFAULT_JWT_SECRET,
    DEFAULT_MAGIC_LINK_BASE_URL,
    DEFAULT_MAGIC_LINK_EMAIL_FROM_EMAIL,
    DEFAULT_MAGIC_LINK_EMAIL_PROVIDER,
    DEFAULT_STRIPE_CONNECT_AUTHORIZE_URL,
    DEFAULT_STRIPE_CONNECT_CLIENT_ID,
    DEFAULT_STRIPE_CONNECT_REDIRECT_URI,
    DEFAULT_STRIPE_SECRET_KEY,
    DEFAULT_STRIPE_WEBHOOK_SECRET,
    PAYPAL_ENVIRONMENT_LIVE,
    PAYPAL_ENVIRONMENT_SANDBOX,
    DEFAULT_TRACKED_LINK_BASE_URL,
    Settings,
    SettingsValidationError,
    get_settings,
    PAYPAL_LIVE_CREATOR_ACCESS_OPERATOR_ONLY,
    PAYPAL_LIVE_CREATOR_ACCESS_PUBLIC,
)
from app.main import app

SAFE_NON_LOCAL_ENV = {
    "APP_ENV": "preview",
    "JWT_SECRET": "story55-preview-jwt-secret-0123456789abcdef",
    "STRIPE_CONNECT_CLIENT_ID": "ca_story55_preview_live",
    "STRIPE_SECRET_KEY": "sk_test_story57_preview_live",
    "STRIPE_CONNECT_AUTHORIZE_URL": DEFAULT_STRIPE_CONNECT_AUTHORIZE_URL,
    "STRIPE_CONNECT_REDIRECT_URI": "https://creatortrust.test/stripe/connect/callback",
    "STRIPE_WEBHOOK_SECRET": "whsec_story55_preview_live",
    "CALENDLY_WEBHOOK_SIGNING_KEY": "cal_story55_preview_live",
    "FULLSCOPE_WEBHOOK_SHARED_SECRET": "fullscope_story55_preview_live",
    "TRACKED_LINK_BASE_URL": "https://trk.creatortrust.test",
    "MAGIC_LINK_EMAIL_PROVIDER": "smtp",
    "MAGIC_LINK_BASE_URL": "https://creatortrust.test",
    "MAGIC_LINK_EMAIL_FROM_EMAIL": "auth@creatortrust.co",
    "MAGIC_LINK_EMAIL_SMTP_HOST": "smtp.creatortrust.co",
    "OPERATOR_EMAIL_ALLOWLIST": "ops1@creatortrust.co,ops2@creatortrust.co",
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
        "stripe_secret_key": SAFE_NON_LOCAL_ENV["STRIPE_SECRET_KEY"],
        "stripe_connect_authorize_url": SAFE_NON_LOCAL_ENV["STRIPE_CONNECT_AUTHORIZE_URL"],
        "stripe_connect_redirect_uri": SAFE_NON_LOCAL_ENV["STRIPE_CONNECT_REDIRECT_URI"],
        "stripe_webhook_secret": SAFE_NON_LOCAL_ENV["STRIPE_WEBHOOK_SECRET"],
        "calendly_webhook_signing_key": SAFE_NON_LOCAL_ENV["CALENDLY_WEBHOOK_SIGNING_KEY"],
        "fullscope_webhook_shared_secret": SAFE_NON_LOCAL_ENV[
            "FULLSCOPE_WEBHOOK_SHARED_SECRET"
        ],
        "tracked_link_base_url": SAFE_NON_LOCAL_ENV["TRACKED_LINK_BASE_URL"],
        "magic_link_email_provider": SAFE_NON_LOCAL_ENV["MAGIC_LINK_EMAIL_PROVIDER"],
        "magic_link_base_url": SAFE_NON_LOCAL_ENV["MAGIC_LINK_BASE_URL"],
        "magic_link_email_from_email": SAFE_NON_LOCAL_ENV["MAGIC_LINK_EMAIL_FROM_EMAIL"],
        "magic_link_email_smtp_host": SAFE_NON_LOCAL_ENV["MAGIC_LINK_EMAIL_SMTP_HOST"],
        "operator_email_allowlist": SAFE_NON_LOCAL_ENV["OPERATOR_EMAIL_ALLOWLIST"],
    }
    data.update(overrides)
    return Settings.model_validate(data)


def _set_non_local_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env_values = dict(SAFE_NON_LOCAL_ENV)
    env_values.update(overrides)
    for name, value in env_values.items():
        monkeypatch.setenv(name, value)


def test_local_defaults_pass_runtime_validation():
    settings = Settings(_env_file=None, app_env="local")

    settings.validate_runtime()


def test_non_local_defaults_fail_with_clear_field_names(monkeypatch: pytest.MonkeyPatch):
    for env_name in (
        "JWT_SECRET",
        "STRIPE_WEBHOOK_SECRET",
        "CALENDLY_WEBHOOK_SIGNING_KEY",
        "FULLSCOPE_WEBHOOK_SHARED_SECRET",
        "STRIPE_CONNECT_CLIENT_ID",
        "STRIPE_SECRET_KEY",
        "STRIPE_CONNECT_REDIRECT_URI",
        "TRACKED_LINK_BASE_URL",
        "MAGIC_LINK_EMAIL_PROVIDER",
        "MAGIC_LINK_BASE_URL",
        "MAGIC_LINK_EMAIL_FROM_EMAIL",
        "MAGIC_LINK_EMAIL_SMTP_HOST",
        "OPERATOR_EMAIL_ALLOWLIST",
    ):
        monkeypatch.delenv(env_name, raising=False)

    settings = Settings(_env_file=None, app_env="preview")

    with pytest.raises(SettingsValidationError) as exc_info:
        settings.validate_runtime()

    message = str(exc_info.value)
    assert "jwt_secret" in message
    assert "stripe_webhook_secret" in message
    assert "calendly_webhook_signing_key" in message
    assert "fullscope_webhook_shared_secret" in message
    assert "stripe_connect_client_id" in message
    assert "stripe_secret_key" in message
    assert "stripe_connect_redirect_uri" in message
    assert "tracked_link_base_url" in message
    assert "operator_email_allowlist" in message
    assert "magic_link_email_provider" in message or "magic_link_base_url" in message
    assert DEFAULT_JWT_SECRET not in message
    assert DEFAULT_STRIPE_WEBHOOK_SECRET not in message
    assert DEFAULT_CALENDLY_WEBHOOK_SIGNING_KEY not in message
    assert DEFAULT_FULLSCOPE_WEBHOOK_SHARED_SECRET not in message
    assert DEFAULT_STRIPE_CONNECT_CLIENT_ID not in message
    assert DEFAULT_STRIPE_SECRET_KEY not in message
    assert DEFAULT_STRIPE_CONNECT_REDIRECT_URI not in message
    assert DEFAULT_TRACKED_LINK_BASE_URL not in message
    assert DEFAULT_MAGIC_LINK_EMAIL_PROVIDER not in message
    assert DEFAULT_MAGIC_LINK_BASE_URL not in message
    assert DEFAULT_MAGIC_LINK_EMAIL_FROM_EMAIL not in message


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


def test_non_local_stub_email_provider_fails_runtime_validation():
    settings = _safe_non_local_settings(magic_link_email_provider="stub")

    with pytest.raises(SettingsValidationError, match="magic_link_email_provider"):
        settings.validate_runtime()


def test_non_local_missing_operator_allowlist_fails_runtime_validation():
    settings = _safe_non_local_settings(operator_email_allowlist="")

    with pytest.raises(SettingsValidationError, match="operator_email_allowlist"):
        settings.validate_runtime()


def test_invalid_paypal_environment_fails_runtime_validation():
    settings = _safe_non_local_settings(paypal_environment="bogus")

    with pytest.raises(SettingsValidationError, match="paypal_environment"):
        settings.validate_runtime()


def test_invalid_paypal_live_creator_access_fails_runtime_validation():
    settings = _safe_non_local_settings(paypal_live_creator_access="bogus")

    with pytest.raises(SettingsValidationError, match="paypal_live_creator_access"):
        settings.validate_runtime()


def test_selected_paypal_settings_follow_explicit_environment():
    settings = Settings.model_validate(
        {
            "app_env": "local",
            "paypal_environment": PAYPAL_ENVIRONMENT_LIVE,
            "paypal_sandbox_client_id": "sandbox-client",
            "paypal_sandbox_client_secret": "sandbox-secret",
            "paypal_sandbox_partner_id": "sandbox-partner",
            "paypal_sandbox_api_base_url": "https://api-m.sandbox.paypal.com",
            "paypal_sandbox_webhook_id": "WH_sandbox",
            "paypal_live_client_id": "live-client",
            "paypal_live_client_secret": "live-secret",
            "paypal_live_partner_id": "live-partner",
            "paypal_live_api_base_url": "https://api-m.paypal.com",
            "paypal_live_webhook_id": "WH_live",
        }
    )

    assert settings.paypal_environment_value() == PAYPAL_ENVIRONMENT_LIVE
    assert settings.selected_paypal_client_id() == "live-client"
    assert settings.selected_paypal_client_secret() == "live-secret"
    assert settings.selected_paypal_partner_id() == "live-partner"
    assert settings.selected_paypal_api_base_url() == "https://api-m.paypal.com"
    assert settings.selected_paypal_webhook_id() == "WH_live"

    sandbox_settings = Settings.model_validate(
        {
            "app_env": "local",
            "paypal_environment": PAYPAL_ENVIRONMENT_SANDBOX,
            "paypal_sandbox_client_id": "sandbox-client",
            "paypal_sandbox_client_secret": "sandbox-secret",
            "paypal_sandbox_partner_id": "sandbox-partner",
            "paypal_sandbox_api_base_url": "https://api-m.sandbox.paypal.com",
            "paypal_sandbox_webhook_id": "WH_sandbox",
            "paypal_live_client_id": "live-client",
            "paypal_live_client_secret": "live-secret",
            "paypal_live_partner_id": "live-partner",
            "paypal_live_api_base_url": "https://api-m.paypal.com",
            "paypal_live_webhook_id": "WH_live",
        }
    )

    assert sandbox_settings.paypal_environment_value() == PAYPAL_ENVIRONMENT_SANDBOX
    assert sandbox_settings.selected_paypal_client_id() == "sandbox-client"
    assert sandbox_settings.selected_paypal_client_secret() == "sandbox-secret"
    assert sandbox_settings.selected_paypal_partner_id() == "sandbox-partner"
    assert sandbox_settings.selected_paypal_api_base_url() == "https://api-m.sandbox.paypal.com"
    assert sandbox_settings.selected_paypal_webhook_id() == "WH_sandbox"


def test_paypal_live_creator_access_defaults_to_operator_only():
    settings = Settings.model_validate(
        {
            "app_env": "local",
            "paypal_environment": PAYPAL_ENVIRONMENT_LIVE,
        }
    )

    assert settings.paypal_live_creator_access_value() == PAYPAL_LIVE_CREATOR_ACCESS_OPERATOR_ONLY


def test_paypal_live_available_to_creator_follows_live_access_mode():
    operator_only_settings = Settings.model_validate(
        {
            "app_env": "local",
            "paypal_environment": PAYPAL_ENVIRONMENT_LIVE,
            "paypal_live_creator_access": PAYPAL_LIVE_CREATOR_ACCESS_OPERATOR_ONLY,
            "operator_email_allowlist": "ops1@creatortrust.co",
        }
    )

    assert operator_only_settings.paypal_live_available_to_creator("ops1@creatortrust.co")
    assert not operator_only_settings.paypal_live_available_to_creator("creator@example.com")
    assert not operator_only_settings.paypal_live_available_to_creator(None)

    public_settings = Settings.model_validate(
        {
            "app_env": "local",
            "paypal_environment": PAYPAL_ENVIRONMENT_LIVE,
            "paypal_live_creator_access": PAYPAL_LIVE_CREATOR_ACCESS_PUBLIC,
            "operator_email_allowlist": "ops1@creatortrust.co",
        }
    )

    assert public_settings.paypal_live_available_to_creator("creator@example.com")

    sandbox_settings = Settings.model_validate(
        {
            "app_env": "local",
            "paypal_environment": PAYPAL_ENVIRONMENT_SANDBOX,
            "paypal_live_creator_access": PAYPAL_LIVE_CREATOR_ACCESS_OPERATOR_ONLY,
        }
    )

    assert sandbox_settings.paypal_live_available_to_creator("creator@example.com")


def test_operator_email_allowlist_parses_comma_separated_values():
    settings = Settings.model_validate(
        {
            "app_env": "local",
            "operator_email_allowlist": " Ops1@CreatorTrust.co, ops2@creatortrust.co , ops1@creatortrust.co ",
        }
    )

    assert settings.operator_email_allowlist_values() == (
        "ops1@creatortrust.co",
        "ops2@creatortrust.co",
    )
    assert settings.is_operator_email_allowed("OPS2@CREATORTRUST.CO")
