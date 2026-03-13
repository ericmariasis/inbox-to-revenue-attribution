import os
from unittest.mock import patch

import pytest

from app.core.config import get_settings
from app.core.startup_smoke import StartupSmokeError, run_startup_smoke


SAFE_NON_LOCAL_ENV = {
    "APP_ENV": "production",
    "DATABASE_URL": "",
    "JWT_SECRET": "story73-production-jwt-secret-0123456789abcdef",
    "STRIPE_CONNECT_CLIENT_ID": "ca_story73_beta_live",
    "STRIPE_SECRET_KEY": "sk_test_story73_beta_live",
    "STRIPE_CONNECT_AUTHORIZE_URL": "https://connect.stripe.com/oauth/authorize",
    "STRIPE_CONNECT_REDIRECT_URI": "https://creatorbeta.co/stripe/connect/callback",
    "STRIPE_WEBHOOK_SECRET": "whsec_story73_beta_live",
    "CALENDLY_WEBHOOK_SIGNING_KEY": "cal_story73_beta_live",
    "TRACKED_LINK_BASE_URL": "https://creatorbeta.co",
    "MAGIC_LINK_EMAIL_PROVIDER": "smtp",
    "MAGIC_LINK_BASE_URL": "https://creatorbeta.co",
    "MAGIC_LINK_EMAIL_FROM_EMAIL": "auth@creatorbeta.co",
    "MAGIC_LINK_EMAIL_SMTP_HOST": "smtp.creatorbeta.co",
    "MAGIC_LINK_EMAIL_SMTP_USERNAME": "smtp-user",
    "MAGIC_LINK_EMAIL_SMTP_PASSWORD": "smtp-password",
}


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _set_safe_non_local_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env_values = dict(SAFE_NON_LOCAL_ENV)
    env_values["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or get_settings().database_url
    env_values.update(overrides)
    for name, value in env_values.items():
        monkeypatch.setenv(name, value)


def test_run_startup_smoke_validates_runtime_and_connects_to_database(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_safe_non_local_env(monkeypatch)

    run_startup_smoke()


def test_run_startup_smoke_wraps_database_connection_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_safe_non_local_env(monkeypatch)

    class _BrokenEngine:
        def connect(self):
            raise RuntimeError("boom")

        def dispose(self):
            return None

    with patch("app.core.startup_smoke.create_engine", return_value=_BrokenEngine()):
        with pytest.raises(StartupSmokeError, match="startup smoke failed"):
            run_startup_smoke()
