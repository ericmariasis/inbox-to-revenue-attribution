from datetime import date, datetime

from pydantic import BaseModel


class ReportsSummaryRowResponse(BaseModel):
    content_id: str
    booking_link_id: str
    tid: str
    source_url: str
    paid_revenue_cents: int
    paid_invoice_count: int
    paid_booking_count: int
    first_paid_at: datetime
    last_paid_at: datetime


class ReportsUnattributedReasonCountResponse(BaseModel):
    reason: str | None
    event_count: int


class ReportsUnattributedBacklogResponse(BaseModel):
    scope: str
    event_count: int
    reasons: list[ReportsUnattributedReasonCountResponse]


class ReportsBlockedReasonCountResponse(BaseModel):
    reason_code: str
    case_count: int


class ReportsBlockedSummaryResponse(BaseModel):
    supported: bool
    reason: str | None = None
    open_case_count: int = 0
    reasons: list[ReportsBlockedReasonCountResponse] = []


class ReportsSummaryResponse(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
    rows: list[ReportsSummaryRowResponse]
    paid_revenue_cents: int
    paid_invoice_count: int
    paid_booking_count: int
    unattributed_current_backlog: ReportsUnattributedBacklogResponse
    blocked_summary: ReportsBlockedSummaryResponse
