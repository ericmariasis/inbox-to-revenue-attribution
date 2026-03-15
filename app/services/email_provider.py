import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from typing import Protocol

from app.core.config import Settings
from app.services.email_stub import send_magic_link_email, send_support_request_email


class MagicLinkEmailDeliveryError(RuntimeError):
    pass


class SupportRequestEmailDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class MagicLinkEmailMessage:
    email: str
    raw_token: str
    magic_link_url: str
    expires_in_minutes: int


@dataclass(frozen=True)
class SupportRequestEmailMessage:
    request_id: str
    support_email: str
    request_type: str
    requester_email: str
    creator_name: str
    creator_id: str
    requested_at: str
    subject: str
    body: str


class EmailProvider(Protocol):
    def send_magic_link(self, message: MagicLinkEmailMessage) -> None: ...
    def send_support_request(self, message: SupportRequestEmailMessage) -> None: ...


class StubEmailProvider:
    def send_magic_link(self, message: MagicLinkEmailMessage) -> None:
        send_magic_link_email(
            email=message.email,
            token=message.raw_token,
            magic_link_url=message.magic_link_url,
        )

    def send_support_request(self, message: SupportRequestEmailMessage) -> None:
        send_support_request_email(
            request_id=message.request_id,
            support_email=message.support_email,
            request_type=message.request_type,
            requester_email=message.requester_email,
            creator_name=message.creator_name,
            creator_id=message.creator_id,
            requested_at=message.requested_at,
            subject=message.subject,
            body=message.body,
        )


class SmtpEmailProvider:
    def __init__(self, *, settings: Settings):
        self._host = settings.magic_link_email_smtp_host.strip()
        self._port = settings.magic_link_email_smtp_port
        self._username = settings.magic_link_email_smtp_username.strip() or None
        self._password = settings.magic_link_email_smtp_password.strip() or None
        self._starttls = settings.magic_link_email_smtp_starttls
        self._use_ssl = settings.magic_link_email_smtp_use_ssl
        self._timeout_seconds = settings.magic_link_email_smtp_timeout_seconds
        self._from_email = str(settings.magic_link_email_from_email)
        self._from_name = settings.magic_link_email_from_name.strip()

    def send_magic_link(self, message: MagicLinkEmailMessage) -> None:
        try:
            self._send_email(
                to_email=message.email,
                subject="Your sign-in link",
                body=_magic_link_email_body(message),
            )
        except (smtplib.SMTPException, OSError) as exc:
            raise MagicLinkEmailDeliveryError("magic-link email delivery failed") from exc

    def send_support_request(self, message: SupportRequestEmailMessage) -> None:
        try:
            self._send_email(
                to_email=message.support_email,
                subject=message.subject,
                body=message.body,
            )
        except (smtplib.SMTPException, OSError) as exc:
            raise SupportRequestEmailDeliveryError("support-request email delivery failed") from exc

    def _maybe_login(self, client: smtplib.SMTP) -> None:
        if self._username is None or self._password is None:
            return
        client.login(self._username, self._password)

    def _send_email(self, *, to_email: str, subject: str, body: str) -> None:
        email_message = EmailMessage()
        email_message["To"] = to_email
        email_message["From"] = (
            formataddr((self._from_name, self._from_email))
            if self._from_name
            else self._from_email
        )
        email_message["Subject"] = subject
        email_message.set_content(body)

        if self._use_ssl:
            with smtplib.SMTP_SSL(
                host=self._host,
                port=self._port,
                timeout=self._timeout_seconds,
            ) as client:
                self._maybe_login(client)
                client.send_message(email_message)
            return

        with smtplib.SMTP(
            host=self._host,
            port=self._port,
            timeout=self._timeout_seconds,
        ) as client:
            client.ehlo()
            if self._starttls:
                client.starttls()
                client.ehlo()
            self._maybe_login(client)
            client.send_message(email_message)


def build_default_email_provider(*, settings: Settings) -> EmailProvider:
    provider_name = settings.magic_link_email_provider.strip().lower()
    if provider_name == "smtp":
        return SmtpEmailProvider(settings=settings)
    return StubEmailProvider()


def _magic_link_email_body(message: MagicLinkEmailMessage) -> str:
    return (
        "Use this secure sign-in link:\n\n"
        f"{message.magic_link_url}\n\n"
        f"This link expires in {message.expires_in_minutes} minutes and can only be used once.\n"
        "If you did not request this email, you can ignore it."
    )
