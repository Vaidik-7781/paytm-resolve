"""Domain and runtime models for Paytm Resolve."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------
# Mock "Paytm world" entities (systems of record the agents act on)
# --------------------------------------------------------------------------
class Party(BaseModel):
    id: str
    kind: Literal["customer", "merchant"]
    name: str
    language: str = "en"  # en | hi | hinglish
    phone_mask: str = "XXXXXX0000"


class Txn(BaseModel):
    id: str
    payer_id: str
    merchant_id: str
    amount: float
    bank: str
    handle: str = "@alpha"
    debit: Literal["DEBITED", "NOT_DEBITED"] = "DEBITED"
    credit: Literal["CREDITED", "FAILED", "PENDING"] = "CREDITED"
    status: Literal["SUCCESS", "FAILED", "PENDING"] = "SUCCESS"
    created_at: datetime
    reversal: Optional[dict] = None  # {status, initiated_at, eta_at}
    refund_id: Optional[str] = None
    order_ref: Optional[str] = None


class Settlement(BaseModel):
    id: str
    merchant_id: str
    amount: float
    status: Literal["SETTLED", "HELD", "PENDING"] = "PENDING"
    hold_reason: Optional[str] = None  # BANK_NAME_MISMATCH | KYC_EXPIRED | RISK_FRAUD_REVIEW | INVALID_IFSC
    created_at: datetime
    released_at: Optional[datetime] = None


class Kyc(BaseModel):
    merchant_id: str
    status: Literal["VERIFIED", "REVERIFICATION_REQUIRED", "EXPIRED"] = "VERIFIED"
    legal_name: str
    bank_account_name: str
    name_match: float = 1.0
    reverification: Literal["NONE", "IN_PROGRESS", "PASSED", "FAILED"] = "NONE"
    risk_flags: list[str] = Field(default_factory=list)


class Dispute(BaseModel):
    id: str
    txn_id: str
    merchant_id: str
    amount: float
    reason: str
    deadline: datetime
    status: Literal["OPEN", "REPRESENTMENT_FILED", "WON", "LOST"] = "OPEN"
    evidence_pack: Optional[dict] = None


class Loan(BaseModel):
    id: str
    party_id: str
    emi_amount: float
    due_date: datetime
    status: Literal["DUE", "OVERDUE", "PAID"] = "DUE"
    outstanding: float = 0.0
    reschedules_last_12m: int = 0
    marketing_consent: bool = False


class Claim(BaseModel):
    id: str
    party_id: str
    kind: str = "health"
    status: Literal["DOCS_PENDING", "IN_REVIEW", "APPROVED"] = "IN_REVIEW"
    missing_docs: list[str] = Field(default_factory=list)
    requested_docs: list[str] = Field(default_factory=list)
    eta_days: int = 3


class Device(BaseModel):
    id: str
    merchant_id: str
    status: Literal["ACTIVE", "OFFLINE", "REPLACEMENT_DISPATCHED"] = "ACTIVE"
    last_seen: datetime
    firmware: str = "1.4.2"
    warranty_active: bool = True
    reboot_restores: bool = True  # simulation switch


class Notification(BaseModel):
    id: str
    party_id: str
    lang: str
    kind: str
    text: str
    ts: datetime
    case_id: Optional[str] = None


class Ticket(BaseModel):
    id: str
    case_id: Optional[str]
    queue: str
    priority: Literal["P1", "P2", "P3"] = "P3"
    summary: str
    status: Literal["OPEN", "CLOSED"] = "OPEN"
    ts: datetime


class Incident(BaseModel):
    id: str
    cluster: str  # e.g. bank name
    kind: str = "ISSUER_DEGRADATION"
    opened_at: datetime
    failure_count: int
    failure_rate: float
    affected_txns: list[str] = Field(default_factory=list)
    proactive_notices: int = 0
    status: Literal["OPEN", "RESOLVED"] = "OPEN"
    suspected_cause: str = ""


# --------------------------------------------------------------------------
# Runtime: cases, traces, plans, policy
# --------------------------------------------------------------------------
class TraceEvent(BaseModel):
    id: str = Field(default_factory=lambda: uid("tr"))
    case_id: str
    ts: datetime = Field(default_factory=utcnow)
    agent: str
    kind: str
    title: str
    detail: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class ProposedAction(BaseModel):
    id: str = Field(default_factory=lambda: uid("act"))
    tool: str
    args: dict[str, Any]
    label: str
    value: float = 0.0
    reversible: bool = True
    kind: Literal["service", "sales", "notify", "escalate"] = "service"
    precondition: Optional[str] = None  # key of a prior result that must be truthy
    expected_effect: str = ""
    preview: dict[str, Any] = Field(default_factory=dict)  # dry-run diff


class ActionRecord(BaseModel):
    id: str
    tool: str
    label: str
    args: dict[str, Any]
    status: Literal["PROPOSED", "EXECUTED", "FAILED", "UNDONE", "SKIPPED"]
    result: dict[str, Any] = Field(default_factory=dict)
    value: float = 0.0
    reversible: bool = True
    compensation_token: Optional[str] = None
    tier: int = 0
    approved_by: Optional[str] = None
    ts: datetime = Field(default_factory=utcnow)


class RootCause(BaseModel):
    code: str
    confidence: float
    rationale: str
    category: str
    citations: list[str] = Field(default_factory=list)
    signals: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(BaseModel):
    tier: int
    label: str
    auto: bool
    requires_human: bool
    reasons: list[dict[str, str]]  # {rule, effect, detail}
    version: str = "policy-v3"
    constraints: dict[str, Any] = Field(default_factory=dict)


class CriticVerdict(BaseModel):
    verdict: Literal["approve", "revise", "reject"]
    checks: list[dict[str, Any]]
    issues: list[str] = Field(default_factory=list)
    confidence_delta: float = 0.0
    llm_second_opinion: Optional[str] = None


class Case(BaseModel):
    id: str = Field(default_factory=lambda: uid("case"))
    party_id: str
    party_name: str = ""
    party_kind: str = "customer"
    channel: str = "chat"  # chat | voice | proactive | event
    message: str = ""
    language: str = "en"
    status: Literal[
        "OPEN", "WORKING", "RESOLVED", "AWAITING_APPROVAL", "AWAITING_CUSTOMER", "ESCALATED", "CLOSED"
    ] = "OPEN"
    intent: Optional[str] = None
    category: Optional[str] = None
    root_cause: Optional[RootCause] = None
    tier: Optional[int] = None
    policy: Optional[PolicyDecision] = None
    critic: Optional[CriticVerdict] = None
    actions: list[ActionRecord] = Field(default_factory=list)
    proposed: list[ProposedAction] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    trace: list[TraceEvent] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    brief: Optional[dict[str, Any]] = None
    approval: Optional[dict[str, Any]] = None
    pending_consent: Optional[dict[str, Any]] = None
    scenario: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    resolved_at: Optional[datetime] = None
    duration_s: Optional[float] = None
    tool_calls: int = 0
    llm_calls: int = 0
    summary: str = ""
    audit_head: Optional[str] = None
