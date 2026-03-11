import argparse
import uuid

from app.services.calendly_webhooks import reprocess_calendly_webhook_event


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reprocess one stored Calendly webhook journal row by journal id."
    )
    parser.add_argument(
        "--journal-id",
        required=True,
        help="The stored Calendly journal row id to reprocess.",
    )
    args = parser.parse_args()

    result = reprocess_calendly_webhook_event(record_id=uuid.UUID(args.journal_id))
    if result.outcome == "missing":
        print(f"missing calendly journal row: {args.journal_id}")
        return 1

    print(
        f"reprocessed calendly journal row {args.journal_id} "
        f"with status={result.processing_status}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
