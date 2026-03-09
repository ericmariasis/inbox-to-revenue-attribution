from unittest.mock import MagicMock, patch

import pytest

from app.core.config import Settings
from app.services.email_provider import (
    MagicLinkEmailDeliveryError,
    MagicLinkEmailMessage,
    SmtpEmailProvider,
    StubEmailProvider,
    build_default_email_provider,
)


def _smtp_settings(**overrides) -> Settings:
    data = {
        "app_env": "preview",
        "magic_link_email_provider": "smtp",
        "magic_link_base_url": "https://creatortrust.test",
        "magic_link_email_from_email": "auth@creatortrust.co",
        "magic_link_email_from_name": "Creator Compass",
        "magic_link_email_smtp_host": "smtp.creatortrust.co",
        "magic_link_email_smtp_port": 587,
        "magic_link_email_smtp_username": "smtp-user",
        "magic_link_email_smtp_password": "smtp-pass",
        "magic_link_email_smtp_starttls": True,
        "magic_link_email_smtp_use_ssl": False,
        "magic_link_email_smtp_timeout_seconds": 10,
    }
    data.update(overrides)
    return Settings.model_validate(data)


def test_build_default_email_provider_uses_stub_for_local_defaults():
    provider = build_default_email_provider(settings=Settings.model_validate({"app_env": "local"}))

    assert isinstance(provider, StubEmailProvider)


def test_smtp_provider_sends_magic_link_email_without_live_network():
    provider = build_default_email_provider(settings=_smtp_settings())
    assert isinstance(provider, SmtpEmailProvider)

    smtp_client = MagicMock()
    message = MagicLinkEmailMessage(
        email="creator@example.com",
        raw_token="raw-token-for-provider-test",
        magic_link_url="https://creatortrust.test/auth/magic-link/verify?token=raw-token-for-provider-test",
        expires_in_minutes=15,
    )

    with patch("app.services.email_provider.smtplib.SMTP") as smtp_factory:
        smtp_factory.return_value.__enter__.return_value = smtp_client
        provider.send_magic_link(message)

    smtp_factory.assert_called_once_with(
        host="smtp.creatortrust.co",
        port=587,
        timeout=10,
    )
    smtp_client.starttls.assert_called_once()
    smtp_client.login.assert_called_once_with("smtp-user", "smtp-pass")
    sent_message = smtp_client.send_message.call_args.args[0]
    assert sent_message["To"] == "creator@example.com"
    assert sent_message["Subject"] == "Your sign-in link"
    assert "https://creatortrust.test/auth/magic-link/verify?token=raw-token-for-provider-test" in sent_message.get_content()
    assert "This link expires in 15 minutes" in sent_message.get_content()


def test_smtp_provider_wraps_delivery_errors():
    provider = SmtpEmailProvider(settings=_smtp_settings())
    message = MagicLinkEmailMessage(
        email="creator@example.com",
        raw_token="raw-token-for-provider-test",
        magic_link_url="https://creatortrust.test/auth/magic-link/verify?token=raw-token-for-provider-test",
        expires_in_minutes=15,
    )

    with patch("app.services.email_provider.smtplib.SMTP", side_effect=OSError("socket closed")):
        with pytest.raises(MagicLinkEmailDeliveryError, match="magic-link email delivery failed"):
            provider.send_magic_link(message)
