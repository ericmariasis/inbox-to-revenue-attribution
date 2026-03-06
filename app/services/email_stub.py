import logging

logger = logging.getLogger(__name__)

def send_magic_link_email(email: str, token: str) -> None:
    # Stubbed provider for Story 4; do not log token or hash values.
    logger.info("magic_link_email_stub_sent email=%s", email)