from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_auth_user
from app.db.session import get_db
from app.models.auth_user import AuthUser
from app.schemas.reporting import (
    ReportsBlockedReasonCountResponse,
    ReportsBlockedSummaryResponse,
    ReportsSummaryResponse,
    ReportsSummaryRowResponse,
    ReportsUnattributedBacklogResponse,
    ReportsUnattributedReasonCountResponse,
)
from app.services.reporting import CreatorReportsSummary, get_creator_reports_summary

router = APIRouter(prefix="/reports", tags=["reports"])


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _build_reports_summary_response(summary: CreatorReportsSummary) -> ReportsSummaryResponse:
    return ReportsSummaryResponse(
        start_date=summary.start_date,
        end_date=summary.end_date,
        rows=[
            ReportsSummaryRowResponse(
                content_id=str(row.content_id),
                booking_link_id=str(row.booking_link_id),
                tid=row.tid,
                source_url=row.source_url,
                paid_revenue_cents=row.paid_revenue_cents,
                paid_invoice_count=row.paid_invoice_count,
                paid_booking_count=row.paid_booking_count,
                first_paid_at=_as_utc(row.first_paid_at),
                last_paid_at=_as_utc(row.last_paid_at),
            )
            for row in summary.rows
        ],
        paid_revenue_cents=summary.paid_revenue_cents,
        paid_invoice_count=summary.paid_invoice_count,
        paid_booking_count=summary.paid_booking_count,
        unattributed_current_backlog=ReportsUnattributedBacklogResponse(
            scope=summary.unattributed_current_backlog.scope,
            event_count=summary.unattributed_current_backlog.event_count,
            reasons=[
                ReportsUnattributedReasonCountResponse(
                    reason=item.reason,
                    event_count=item.event_count,
                )
                for item in summary.unattributed_current_backlog.reasons
            ],
        ),
        blocked_summary=ReportsBlockedSummaryResponse(
            supported=summary.blocked_summary.supported,
            reason=summary.blocked_summary.reason,
            open_case_count=summary.blocked_summary.open_case_count,
            reasons=[
                ReportsBlockedReasonCountResponse(
                    reason_code=item.reason_code,
                    case_count=item.case_count,
                )
                for item in summary.blocked_summary.reasons
            ],
        ),
    )


@router.get("/summary", response_model=ReportsSummaryResponse)
def get_reports_summary(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    current_user: AuthUser = Depends(get_current_auth_user),
    db: Session = Depends(get_db),
) -> ReportsSummaryResponse:
    try:
        summary = get_creator_reports_summary(
            creator_id=current_user.creator_id,
            db=db,
            start_date=start_date,
            end_date=end_date,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return _build_reports_summary_response(summary)
