import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.services.email_provider import (
    EmailProvider,
    SupportRequestEmailDeliveryError,
    SupportRequestEmailMessage,
)

logger = logging.getLogger(__name__)

SUPPORT_REQUEST_TYPE_WORKSPACE_RESET = "workspace-reset"
SUPPORT_REQUEST_TYPE_ACCOUNT_DELETION = "account-deletion"
ACCOUNT_SUPPORT_EMAIL = "eric@careercodepro.com"


@dataclass(frozen=True)
class SupportRequestSubmission:
    request_type: str
    creator_id: str
    creator_name: str
    requester_email: str


def send_support_request_email(
    *,
    provider: EmailProvider,
    submission: SupportRequestSubmission,
) -> None:
    requested_at = datetime.now(timezone.utc).replace(microsecond=0)
    message = SupportRequestEmailMessage(
        support_email=ACCOUNT_SUPPORT_EMAIL,
        request_type=submission.request_type,
        requester_email=submission.requester_email,
        creator_name=submission.creator_name,
        creator_id=submission.creator_id,
        requested_at=requested_at.isoformat(),
        subject=_support_request_subject(submission.request_type, submission.requester_email),
        body=_support_request_body(
            request_type=submission.request_type,
            requester_email=submission.requester_email,
            creator_name=submission.creator_name,
            creator_id=submission.creator_id,
            requested_at=requested_at,
        ),
    )

    try:
        provider.send_support_request(message)
    except SupportRequestEmailDeliveryError:
        logger.warning(
            "support_request_delivery_failed request_type=%s support_email=%s requester_email=%s creator_id=%s",
            submission.request_type,
            ACCOUNT_SUPPORT_EMAIL,
            submission.requester_email,
            submission.creator_id,
        )
        raise

    logger.info(
        "support_request_delivery_succeeded request_type=%s support_email=%s requester_email=%s creator_id=%s",
        submission.request_type,
        ACCOUNT_SUPPORT_EMAIL,
        submission.requester_email,
        submission.creator_id,
    )


def _support_request_subject(request_type: str, requester_email: str) -> str:
    if request_type == SUPPORT_REQUEST_TYPE_WORKSPACE_RESET:
        return f"Workspace reset request for {requester_email}"
    return f"Account deletion request for {requester_email}"


def _support_request_body(
    *,
    request_type: str,
    requester_email: str,
    creator_name: str,
    creator_id: str,
    requested_at: datetime,
) -> str:
    request_label = "Workspace reset" if request_type == SUPPORT_REQUEST_TYPE_WORKSPACE_RESET else "Account deletion"
    return (
        f"{request_label} request\n\n"
        "This beta request was submitted from the signed-in account page.\n\n"
        f"Request type: {request_type}\n"
        f"Signed-in email: {requester_email}\n"
        f"Workspace name: {creator_name}\n"
        f"Creator id: {creator_id}\n"
        f"Requested at UTC: {requested_at.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
    )
