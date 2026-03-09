import logging

logger = logging.getLogger(__name__)
_magic_link_outbox: list[dict[str, str]] = []


def clear_magic_link_outbox() -> None:
    _magic_link_outbox.clear()


def get_magic_link_outbox() -> list[dict[str, str]]:
    return list(_magic_link_outbox)


def send_magic_link_email(email: str, token: str, *, magic_link_url: str | None = None) -> None:
    message = {"email": email, "token": token}
    if magic_link_url is not None:
        message["magic_link_url"] = magic_link_url
    _magic_link_outbox.append(message)
    # Stubbed provider for Story 4; do not log token or hash values.
    logger.info("magic_link_email_stub_sent email=%s", email)
