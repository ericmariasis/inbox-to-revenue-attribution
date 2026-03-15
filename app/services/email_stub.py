import logging

logger = logging.getLogger(__name__)
_magic_link_outbox: list[dict[str, str]] = []
_support_request_outbox: list[dict[str, str]] = []


def clear_magic_link_outbox() -> None:
    _magic_link_outbox.clear()


def get_magic_link_outbox() -> list[dict[str, str]]:
    return list(_magic_link_outbox)


def clear_support_request_outbox() -> None:
    _support_request_outbox.clear()


def get_support_request_outbox() -> list[dict[str, str]]:
    return list(_support_request_outbox)


def send_magic_link_email(email: str, token: str, *, magic_link_url: str | None = None) -> None:
    message = {"email": email, "token": token}
    if magic_link_url is not None:
        message["magic_link_url"] = magic_link_url
    _magic_link_outbox.append(message)
    # Stubbed provider for Story 4; do not log token or hash values.
    logger.info("magic_link_email_stub_sent email=%s", email)


def send_support_request_email(
    *,
    support_email: str,
    request_type: str,
    requester_email: str,
    creator_name: str,
    creator_id: str,
    requested_at: str,
    subject: str,
    body: str,
) -> None:
    _support_request_outbox.append(
        {
            "support_email": support_email,
            "request_type": request_type,
            "requester_email": requester_email,
            "creator_name": creator_name,
            "creator_id": creator_id,
            "requested_at": requested_at,
            "subject": subject,
            "body": body,
        }
    )
    logger.info(
        "support_request_email_stub_sent request_type=%s support_email=%s requester_email=%s",
        request_type,
        support_email,
        requester_email,
    )
