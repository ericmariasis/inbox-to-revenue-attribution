import argparse
import os
import uuid
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text

from app.core.config import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a creator-scoped booking link and content row for live Calendly validation."
    )
    parser.add_argument(
        "--calendly-url",
        required=True,
        help="Real Calendly scheduling URL to use as the redirect destination.",
    )
    parser.add_argument(
        "--source-url",
        default="https://example.com/live-calendly-validation",
        help="Source content URL to store with the content row.",
    )
    parser.add_argument(
        "--creator-email",
        help="Creator email to insert. Defaults to a generated local-only email.",
    )
    parser.add_argument(
        "--creator-name",
        default="Calendly Live Validation",
        help="Creator name to insert.",
    )
    parser.add_argument(
        "--booking-link-name",
        default="Calendly Live Validation Link",
        help="Booking link name to insert.",
    )
    parser.add_argument(
        "--tracked-base-url",
        help="Tracked-link base URL to print. Defaults to TRACKED_LINK_BASE_URL, then CALENDLY_WEBHOOK_PUBLIC_BASE_URL, then app settings.",
    )
    args = parser.parse_args()

    calendly_url = str(args.calendly_url).strip()
    parsed = urlsplit(calendly_url)
    if parsed.scheme != "https" or parsed.netloc not in {"calendly.com", "www.calendly.com"} or not parsed.path.strip("/"):
        raise SystemExit("error=--calendly-url must be a valid https://calendly.com/... URL")

    settings = get_settings()
    engine = create_engine(settings.database_url)

    creator_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    booking_link_id = str(uuid.uuid4())
    content_id = str(uuid.uuid4())
    tid = uuid.uuid4().hex
    creator_email = args.creator_email or f"live_validation_{uuid.uuid4().hex[:12]}@example.com"
    tracked_base_url = (
        args.tracked_base_url
        or os.getenv("TRACKED_LINK_BASE_URL")
        or os.getenv("CALENDLY_WEBHOOK_PUBLIC_BASE_URL")
        or settings.tracked_link_base_url
    ).rstrip("/")
    tracked_url = f"{tracked_base_url}/r/{tid}"

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO creators (id, name, stripe_connect_status) "
                "VALUES (:id, :name, :stripe_connect_status)"
            ),
            {
                "id": creator_id,
                "name": args.creator_name,
                "stripe_connect_status": "pending",
            },
        )
        conn.execute(
            text(
                "INSERT INTO auth_users (id, creator_id, email) "
                "VALUES (:id, :creator_id, :email)"
            ),
            {
                "id": user_id,
                "creator_id": creator_id,
                "email": creator_email,
            },
        )
        conn.execute(
            text(
                "INSERT INTO booking_links (id, creator_id, name, calendly_url) "
                "VALUES (:id, :creator_id, :name, :calendly_url)"
            ),
            {
                "id": booking_link_id,
                "creator_id": creator_id,
                "name": args.booking_link_name,
                "calendly_url": calendly_url,
            },
        )
        conn.execute(
            text(
                "INSERT INTO content (id, creator_id, booking_link_id, source_url, tid) "
                "VALUES (:id, :creator_id, :booking_link_id, :source_url, :tid)"
            ),
            {
                "id": content_id,
                "creator_id": creator_id,
                "booking_link_id": booking_link_id,
                "source_url": args.source_url,
                "tid": tid,
            },
        )

    print(f"creator_email={creator_email}")
    print(f"creator_id={creator_id}")
    print(f"user_id={user_id}")
    print(f"booking_link_id={booking_link_id}")
    print(f"content_id={content_id}")
    print(f"tid={tid}")
    print(f"tracked_url={tracked_url}")
    print(f"calendly_url={calendly_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
