from unittest.mock import MagicMock, patch

import pytest

from app.core.config import Settings
from app.services.email_provider import (
    MagicLinkEmailDeliveryError,
    MagicLinkEmailMessage,
    SmtpEmailProvider,
    StubEmailProvider,
    SupportRequestEmailDeliveryError,
    SupportRequestEmailMessage,
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
    assert sent_message.is_multipart()

    plain_part = sent_message.get_body(preferencelist=("plain",))
    assert plain_part is not None
    plain_content = plain_part.get_content()
    assert (
        "https://creatortrust.test/auth/magic-link/verify?token=raw-token-for-provider-test"
        in plain_content
    )
    assert "This link expires in 15 minutes" in plain_content

    html_part = sent_message.get_body(preferencelist=("html",))
    assert html_part is not None
    html_content = html_part.get_content()
    assert 'href="https://creatortrust.test/auth/magic-link/verify?token=raw-token-for-provider-test"' in html_content
    assert "Sign in securely" in html_content


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


def test_smtp_provider_sends_support_request_email_without_live_network():
    provider = build_default_email_provider(settings=_smtp_settings())
    assert isinstance(provider, SmtpEmailProvider)

    smtp_client = MagicMock()
    message = SupportRequestEmailMessage(
        request_id="6ba7b810-9dad-11d1-80b4-00c04fd430c8",
        support_email="eric@careercodepro.com",
        request_type="workspace-reset",
        requester_email="creator@example.com",
        creator_name="Creator Example",
        creator_id="creator-123",
        requested_at="2026-03-14T21:30:00+00:00",
        subject="Workspace reset request for creator@example.com",
        body=(
            "Workspace reset request\n\n"
            "This beta request was submitted from the signed-in account page.\n\n"
            "Request id: 6ba7b810-9dad-11d1-80b4-00c04fd430c8\n"
            "Request type: workspace-reset\n"
            "Signed-in email: creator@example.com\n"
        ),
    )

    with patch("app.services.email_provider.smtplib.SMTP") as smtp_factory:
        smtp_factory.return_value.__enter__.return_value = smtp_client
        provider.send_support_request(message)

    smtp_factory.assert_called_once_with(
        host="smtp.creatortrust.co",
        port=587,
        timeout=10,
    )
    smtp_client.starttls.assert_called_once()
    smtp_client.login.assert_called_once_with("smtp-user", "smtp-pass")
    sent_message = smtp_client.send_message.call_args.args[0]
    assert sent_message["To"] == "eric@careercodepro.com"
    assert sent_message["Subject"] == "Workspace reset request for creator@example.com"
    assert not sent_message.is_multipart()
    assert "Request id: 6ba7b810-9dad-11d1-80b4-00c04fd430c8" in sent_message.get_content()
    assert "Request type: workspace-reset" in sent_message.get_content()
    assert "Signed-in email: creator@example.com" in sent_message.get_content()


def test_smtp_provider_wraps_support_request_delivery_errors():
    provider = SmtpEmailProvider(settings=_smtp_settings())
    message = SupportRequestEmailMessage(
        request_id="6ba7b811-9dad-11d1-80b4-00c04fd430c8",
        support_email="eric@careercodepro.com",
        request_type="account-deletion",
        requester_email="creator@example.com",
        creator_name="Creator Example",
        creator_id="creator-123",
        requested_at="2026-03-14T21:30:00+00:00",
        subject="Account deletion request for creator@example.com",
        body="Account deletion request",
    )

    with patch("app.services.email_provider.smtplib.SMTP", side_effect=OSError("socket closed")):
        with pytest.raises(SupportRequestEmailDeliveryError, match="support-request email delivery failed"):
            provider.send_support_request(message)
