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


class BookingAttributionReasonCountResponse(BaseModel):
    reason: str
    booking_count: int


class BookingAttributionHealthResponse(BaseModel):
    unattributed_booking_count: int
    reasons: list[BookingAttributionReasonCountResponse]


class ProviderIngressStatusCountResponse(BaseModel):
    processing_status: str
    event_count: int


class ProviderIngressHealthResponse(BaseModel):
    backlog_event_count: int
    failed_event_count: int
    statuses: list[ProviderIngressStatusCountResponse]


class PaymentProvenanceStateCountResponse(BaseModel):
    state: str
    row_count: int


class PaymentProvenanceReasonCountResponse(BaseModel):
    reason: str | None
    event_count: int


class PaymentProviderHealthResponse(BaseModel):
    payment_provider: str
    settled_state_counts: list["PaymentProvenanceStateCountResponse"]
    current_backlog_event_count: int
    current_backlog_reasons: list["PaymentProvenanceReasonCountResponse"]


class PaymentProvenanceHealthResponse(BaseModel):
    settled_state_counts: list[PaymentProvenanceStateCountResponse]
    current_backlog_event_count: int
    current_backlog_reasons: list[PaymentProvenanceReasonCountResponse]
    provider_health: list[PaymentProviderHealthResponse] = []


class BlockedBillingHealthResponse(BaseModel):
    open_case_count: int
    reasons: list[ReportsBlockedReasonCountResponse]


class AuthoritativeContentLagReasonCountResponse(BaseModel):
    reason: str
    content_count: int


class AuthoritativeContentHealthResponse(BaseModel):
    lagging_content_count: int
    reasons: list[AuthoritativeContentLagReasonCountResponse]


class EvidenceIngressHealthResponse(BaseModel):
    creator_id: str
    booking_attribution: BookingAttributionHealthResponse
    calendly_ingress: ProviderIngressHealthResponse
    fullscope_ingress: ProviderIngressHealthResponse
    payment_provenance: PaymentProvenanceHealthResponse
    blocked_billing: BlockedBillingHealthResponse
    authoritative_content: AuthoritativeContentHealthResponse
