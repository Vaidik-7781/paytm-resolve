"""In-memory mock of the Paytm systems of record (ledger, UPI rail, settlement, KYC, disputes, lending,
insurance, devices, ticketing). Every write goes through the Tool Gateway, never directly from an agent.

All data here is synthetic. Replace `World` with real service clients to go to production; the agents and the
gateway only depend on the method signatures below.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from ..models import (
    Claim, Device, Dispute, Incident, Kyc, Loan, Notification, Party, Settlement, Ticket, Txn, uid, utcnow,
)


class UpstreamError(Exception):
    """Raised by a mock system to simulate transient or hard upstream failures."""

    def __init__(self, code: str, retryable: bool = True):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class World:
    def __init__(self) -> None:
        self.reset()

    # ------------------------------------------------------------------ state
    def reset(self) -> None:
        self.parties: dict[str, Party] = {}
        self.txns: dict[str, Txn] = {}
        self.settlements: dict[str, Settlement] = {}
        self.kyc: dict[str, Kyc] = {}
        self.disputes: dict[str, Dispute] = {}
        self.loans: dict[str, Loan] = {}
        self.claims: dict[str, Claim] = {}
        self.devices: dict[str, Device] = {}
        self.notifications: list[Notification] = []
        self.tickets: list[Ticket] = []
        self.incidents: dict[str, Incident] = {}
        self.order_evidence: dict[str, dict[str, Any]] = {}
        self.bank_state: dict[str, str] = {}  # bank -> OK | DEGRADED
        self.inject_failures: dict[str, int] = {}  # tool -> remaining failures
        self.audit: list[dict[str, Any]] = []
        self._seq = 9000

    def now(self) -> datetime:
        return utcnow()

    def log(self, system: str, event: str, **data: Any) -> None:
        self.audit.append({"ts": self.now().isoformat(), "system": system, "event": event, **data})
        if len(self.audit) > 2000:
            del self.audit[:500]

    def _maybe_fail(self, tool: str) -> None:
        left = self.inject_failures.get(tool, 0)
        if left > 0:
            self.inject_failures[tool] = left - 1
            raise UpstreamError("UPSTREAM_TIMEOUT", retryable=True)

    def next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    # ------------------------------------------------------------------ reads
    def get_party(self, party_id: str) -> Optional[Party]:
        return self.parties.get(party_id)

    def get_txn(self, txn_id: str) -> Optional[Txn]:
        return self.txns.get(txn_id)

    def list_recent_txns(self, party_id: str, days: int = 7) -> list[Txn]:
        cutoff = self.now() - timedelta(days=days)
        out = [t for t in self.txns.values() if (t.payer_id == party_id or t.merchant_id == party_id) and t.created_at >= cutoff]
        return sorted(out, key=lambda t: t.created_at, reverse=True)

    def upi_status(self, txn_id: str) -> dict[str, Any]:
        t = self.txns.get(txn_id)
        if not t:
            raise UpstreamError("TXN_NOT_FOUND", retryable=False)
        age_h = (self.now() - t.created_at).total_seconds() / 3600
        code = "00" if t.credit == "CREDITED" else ("91" if t.credit == "PENDING" else "U16")
        return {
            "txn_id": t.id, "rail_status": t.status, "bank": t.bank, "bank_state": self.bank_state.get(t.bank, "OK"),
            "debit_leg": t.debit, "credit_leg": t.credit, "response_code": code, "age_hours": round(age_h, 1),
            "reversal": t.reversal, "refund_id": t.refund_id,
        }

    def get_settlement(self, settlement_id: str) -> Optional[Settlement]:
        return self.settlements.get(settlement_id)

    def list_settlements(self, merchant_id: str, status: Optional[str] = None) -> list[Settlement]:
        out = [s for s in self.settlements.values() if s.merchant_id == merchant_id and (status is None or s.status == status)]
        return sorted(out, key=lambda s: s.created_at, reverse=True)

    def get_kyc(self, merchant_id: str) -> Optional[Kyc]:
        return self.kyc.get(merchant_id)

    def get_dispute(self, dispute_id: str) -> Optional[Dispute]:
        return self.disputes.get(dispute_id)

    def list_disputes(self, merchant_id: str) -> list[Dispute]:
        return [d for d in self.disputes.values() if d.merchant_id == merchant_id]

    def get_order_evidence(self, txn_id: str) -> dict[str, Any]:
        return self.order_evidence.get(txn_id, {})

    def get_loan(self, loan_id: str) -> Optional[Loan]:
        return self.loans.get(loan_id)

    def list_loans(self, party_id: str) -> list[Loan]:
        return [l for l in self.loans.values() if l.party_id == party_id]

    def get_claim(self, claim_id: str) -> Optional[Claim]:
        return self.claims.get(claim_id)

    def list_claims(self, party_id: str) -> list[Claim]:
        return [c for c in self.claims.values() if c.party_id == party_id]

    def get_device(self, device_id: str) -> Optional[Device]:
        return self.devices.get(device_id)

    def list_devices(self, merchant_id: str) -> list[Device]:
        return [d for d in self.devices.values() if d.merchant_id == merchant_id]

    # ------------------------------------------------------------------ writes (called by gateway handlers only)
    def refund(self, txn_id: str, amount: float, reason: str) -> dict[str, Any]:
        self._maybe_fail("initiate_refund")
        t = self.txns.get(txn_id)
        if not t:
            raise UpstreamError("TXN_NOT_FOUND", retryable=False)
        if t.refund_id:
            raise UpstreamError("ALREADY_REFUNDED", retryable=False)
        if amount > t.amount + 1e-9:
            raise UpstreamError("AMOUNT_EXCEEDS_TXN", retryable=False)
        rid = self.next_id("RF")
        t.refund_id = rid
        t.reversal = {"status": "REFUNDED", "initiated_at": self.now().isoformat(), "refund_id": rid, "amount": amount}
        self.log("ledger", "refund_created", txn=txn_id, refund=rid, amount=amount, reason=reason)
        return {"refund_id": rid, "txn_id": txn_id, "amount": amount, "destination": "ORIGINAL_SOURCE"}

    def cancel_refund(self, refund_id: str) -> dict[str, Any]:
        for t in self.txns.values():
            if t.refund_id == refund_id:
                t.refund_id = None
                t.reversal = None
                self.log("ledger", "refund_cancelled", refund=refund_id)
                return {"cancelled": refund_id}
        raise UpstreamError("REFUND_NOT_FOUND", retryable=False)

    def retry_reversal(self, txn_id: str) -> dict[str, Any]:
        self._maybe_fail("retry_reversal")
        t = self.txns.get(txn_id)
        if not t:
            raise UpstreamError("TXN_NOT_FOUND", retryable=False)
        t.reversal = {"status": "INITIATED", "initiated_at": self.now().isoformat(),
                      "eta_at": (self.now() + timedelta(hours=6)).isoformat()}
        self.log("upi", "reversal_retried", txn=txn_id)
        return {"txn_id": txn_id, "reversal": t.reversal}

    def trigger_reverification(self, merchant_id: str) -> dict[str, Any]:
        self._maybe_fail("trigger_reverification")
        k = self.kyc.get(merchant_id)
        if not k:
            raise UpstreamError("KYC_NOT_FOUND", retryable=False)
        k.reverification = "IN_PROGRESS"
        self.log("kyc", "reverification_started", merchant=merchant_id)
        return {"merchant_id": merchant_id, "state": "IN_PROGRESS"}

    def validate_bank_account(self, merchant_id: str) -> dict[str, Any]:
        """Penny-drop style validation (mock): passes when the bank-side name matches closely enough."""
        self._maybe_fail("validate_bank_account")
        k = self.kyc.get(merchant_id)
        if not k:
            raise UpstreamError("KYC_NOT_FOUND", retryable=False)
        ok = k.name_match >= 0.85 and not k.risk_flags
        k.reverification = "PASSED" if ok else "FAILED"
        if ok:
            k.status = "VERIFIED"
        self.log("kyc", "bank_validation", merchant=merchant_id, ok=ok, score=k.name_match)
        return {"merchant_id": merchant_id, "passed": ok, "name_match": k.name_match, "state": k.reverification}

    def revert_verification(self, merchant_id: str) -> dict[str, Any]:
        k = self.kyc.get(merchant_id)
        if k:
            k.reverification = "NONE"
            k.status = "REVERIFICATION_REQUIRED"
        return {"merchant_id": merchant_id, "state": "NONE"}

    def release_hold(self, settlement_id: str) -> dict[str, Any]:
        self._maybe_fail("release_settlement_hold")
        s = self.settlements.get(settlement_id)
        if not s:
            raise UpstreamError("SETTLEMENT_NOT_FOUND", retryable=False)
        k = self.kyc.get(s.merchant_id)
        if s.hold_reason == "RISK_FRAUD_REVIEW" or (k and k.risk_flags):
            raise UpstreamError("RISK_HOLD_REQUIRES_HUMAN", retryable=False)
        if k and k.reverification != "PASSED":
            raise UpstreamError("PRECONDITION_FAILED_REVERIFICATION", retryable=False)
        s.status = "SETTLED"
        s.hold_reason = None
        s.released_at = self.now()
        self.log("settlement", "hold_released", settlement=settlement_id, amount=s.amount)
        return {"settlement_id": settlement_id, "status": "SETTLED", "amount": s.amount}

    def rehold(self, settlement_id: str, reason: str) -> dict[str, Any]:
        s = self.settlements.get(settlement_id)
        if s:
            s.status = "HELD"
            s.hold_reason = reason
            s.released_at = None
        return {"settlement_id": settlement_id, "status": "HELD"}

    def file_representment(self, dispute_id: str, pack_id: str) -> dict[str, Any]:
        self._maybe_fail("file_representment")
        d = self.disputes.get(dispute_id)
        if not d:
            raise UpstreamError("DISPUTE_NOT_FOUND", retryable=False)
        if self.now() > d.deadline:
            raise UpstreamError("DEADLINE_PASSED", retryable=False)
        d.status = "REPRESENTMENT_FILED"
        self.log("disputes", "representment_filed", dispute=dispute_id, pack=pack_id)
        return {"dispute_id": dispute_id, "status": d.status, "pack_id": pack_id}

    def withdraw_representment(self, dispute_id: str) -> dict[str, Any]:
        d = self.disputes.get(dispute_id)
        if d:
            d.status = "OPEN"
        return {"dispute_id": dispute_id, "status": "OPEN"}

    def reschedule_emi(self, loan_id: str, new_due: datetime) -> dict[str, Any]:
        self._maybe_fail("reschedule_emi")
        l = self.loans.get(loan_id)
        if not l:
            raise UpstreamError("LOAN_NOT_FOUND", retryable=False)
        if l.reschedules_last_12m >= 1:
            raise UpstreamError("RESCHEDULE_LIMIT_REACHED", retryable=False)
        old = l.due_date
        l.due_date = new_due
        l.reschedules_last_12m += 1
        self.log("lending", "emi_rescheduled", loan=loan_id, old=old.isoformat(), new=new_due.isoformat())
        return {"loan_id": loan_id, "old_due": old.isoformat(), "new_due": new_due.isoformat()}

    def restore_emi(self, loan_id: str, old_due: datetime) -> dict[str, Any]:
        l = self.loans.get(loan_id)
        if l:
            l.due_date = old_due
            l.reschedules_last_12m = max(0, l.reschedules_last_12m - 1)
        return {"loan_id": loan_id, "due_date": old_due.isoformat()}

    def request_documents(self, claim_id: str, docs: list[str]) -> dict[str, Any]:
        self._maybe_fail("request_documents")
        c = self.claims.get(claim_id)
        if not c:
            raise UpstreamError("CLAIM_NOT_FOUND", retryable=False)
        c.requested_docs = sorted(set(c.requested_docs) | set(docs))
        self.log("claims", "documents_requested", claim=claim_id, docs=docs)
        return {"claim_id": claim_id, "requested": c.requested_docs}

    def cancel_document_request(self, claim_id: str, docs: list[str]) -> dict[str, Any]:
        c = self.claims.get(claim_id)
        if c:
            c.requested_docs = [d for d in c.requested_docs if d not in docs]
        return {"claim_id": claim_id}

    def reboot_device(self, device_id: str) -> dict[str, Any]:
        self._maybe_fail("remote_reboot")
        d = self.devices.get(device_id)
        if not d:
            raise UpstreamError("DEVICE_NOT_FOUND", retryable=False)
        if d.reboot_restores:
            d.status = "ACTIVE"
            d.last_seen = self.now()
        self.log("devices", "remote_reboot", device=device_id, restored=d.reboot_restores)
        return {"device_id": device_id, "restored": d.reboot_restores, "status": d.status}

    def dispatch_replacement(self, device_id: str) -> dict[str, Any]:
        d = self.devices.get(device_id)
        if not d:
            raise UpstreamError("DEVICE_NOT_FOUND", retryable=False)
        if not d.warranty_active:
            raise UpstreamError("OUT_OF_WARRANTY", retryable=False)
        d.status = "REPLACEMENT_DISPATCHED"
        self.log("devices", "replacement_dispatched", device=device_id)
        return {"device_id": device_id, "status": d.status}

    def cancel_replacement(self, device_id: str) -> dict[str, Any]:
        d = self.devices.get(device_id)
        if d:
            d.status = "OFFLINE"
        return {"device_id": device_id}

    def notify(self, party_id: str, text: str, kind: str, lang: str, case_id: Optional[str]) -> dict[str, Any]:
        n = Notification(id=uid("ntf"), party_id=party_id, lang=lang, kind=kind, text=text, ts=self.now(), case_id=case_id)
        self.notifications.append(n)
        self.log("notify", "sent", party=party_id, kind=kind, case=case_id)
        return {"notification_id": n.id}

    def create_ticket(self, case_id: Optional[str], queue: str, priority: str, summary: str) -> dict[str, Any]:
        tk = Ticket(id=self.next_id("TK"), case_id=case_id, queue=queue, priority=priority, summary=summary, ts=self.now())
        self.tickets.append(tk)
        self.log("ticketing", "created", ticket=tk.id, queue=queue)
        return {"ticket_id": tk.id, "queue": queue}

    # ------------------------------------------------------------------ snapshot for the UI
    def snapshot(self) -> dict[str, Any]:
        def d(m):
            return {k: v.model_dump(mode="json") for k, v in m.items()}

        return {
            "parties": d(self.parties), "txns": d(self.txns), "settlements": d(self.settlements), "kyc": d(self.kyc),
            "disputes": d(self.disputes), "loans": d(self.loans), "claims": d(self.claims), "devices": d(self.devices),
            "tickets": [t.model_dump(mode="json") for t in self.tickets[-30:]],
            "notifications": [n.model_dump(mode="json") for n in self.notifications[-30:]],
            "incidents": d(self.incidents),
        }


# =====================================================================================================
# Fixtures: each scenario (re)creates exactly the entities it needs, so it can be re-run at any time.
# =====================================================================================================
def ago(hours: float = 0, minutes: float = 0) -> datetime:
    return utcnow() - timedelta(hours=hours, minutes=minutes)


def ahead(hours: float = 0, days: float = 0) -> datetime:
    return utcnow() + timedelta(hours=hours, days=days)


def base_parties(w: World) -> None:
    people = [
        ("c_aarav", "customer", "Aarav Mehta", "hinglish"), ("c_diya", "customer", "Diya Nair", "en"),
        ("c_rohan", "customer", "Rohan Das", "en"), ("c_priya", "customer", "Priya Sharma", "hinglish"),
        ("c_meera", "customer", "Meera Iyer", "en"), ("c_zoya", "customer", "Zoya Khan", "en"),
        ("m_sharma", "merchant", "Sharma Kirana Store", "hi"), ("m_nova", "merchant", "Nova Electronics", "en"),
        ("m_urban", "merchant", "UrbanCart Online", "en"), ("m_chai", "merchant", "Chai Point Corner", "hi"),
        ("m_rec", "merchant", "Recipient Unknown", "en"),
    ]
    for pid, kind, name, lang in people:
        w.parties[pid] = Party(id=pid, kind=kind, name=name, language=lang, phone_mask="XXXXXX" + str(abs(hash(pid)) % 9000 + 1000))


def setup_failed_payment_stuck(w: World) -> None:
    w.txns["T-1001"] = Txn(id="T-1001", payer_id="c_aarav", merchant_id="m_sharma", amount=1249, bank="Alpha Bank",
                           handle="@alpha", debit="DEBITED", credit="FAILED", status="FAILED", created_at=ago(30), order_ref="ORD-5521")


def setup_failed_payment_in_tat(w: World) -> None:
    w.txns["T-1002"] = Txn(id="T-1002", payer_id="c_diya", merchant_id="m_chai", amount=799, bank="Beta Bank", handle="@beta",
                           debit="DEBITED", credit="PENDING", status="PENDING", created_at=ago(3),
                           reversal={"status": "INITIATED", "initiated_at": ago(2).isoformat(), "eta_at": ahead(hours=22).isoformat()})


def setup_duplicate_debit(w: World) -> None:
    t0 = ago(minutes=15)
    w.txns["T-1003"] = Txn(id="T-1003", payer_id="c_rohan", merchant_id="m_urban", amount=2499, bank="Gamma Bank", handle="@gamma",
                           debit="DEBITED", credit="CREDITED", status="SUCCESS", created_at=t0, order_ref="ORD-7710")
    w.txns["T-1004"] = Txn(id="T-1004", payer_id="c_rohan", merchant_id="m_urban", amount=2499, bank="Gamma Bank", handle="@gamma",
                           debit="DEBITED", credit="FAILED", status="FAILED", created_at=t0 + timedelta(seconds=40), order_ref="ORD-7710")


def setup_settlement_name_mismatch(w: World) -> None:
    w.settlements["S-5001"] = Settlement(id="S-5001", merchant_id="m_sharma", amount=18400, status="HELD",
                                         hold_reason="BANK_NAME_MISMATCH", created_at=ago(20))
    w.kyc["m_sharma"] = Kyc(merchant_id="m_sharma", status="REVERIFICATION_REQUIRED", legal_name="Sharma Kirana Store",
                            bank_account_name="SHARMA KIRANA STORES", name_match=0.93)


def setup_settlement_fraud_hold(w: World) -> None:
    w.settlements["S-5002"] = Settlement(id="S-5002", merchant_id="m_nova", amount=210000, status="HELD",
                                         hold_reason="RISK_FRAUD_REVIEW", created_at=ago(10))
    w.kyc["m_nova"] = Kyc(merchant_id="m_nova", status="VERIFIED", legal_name="Nova Electronics Pvt Ltd",
                          bank_account_name="NOVA ELECTRONICS PVT LTD", name_match=1.0,
                          risk_flags=["velocity_anomaly", "fraud_review"])


def setup_settlement_kyc_expired(w: World) -> None:
    w.settlements["S-5003"] = Settlement(id="S-5003", merchant_id="m_chai", amount=6200, status="HELD", hold_reason="KYC_EXPIRED",
                                         created_at=ago(30))
    w.kyc["m_chai"] = Kyc(merchant_id="m_chai", status="EXPIRED", legal_name="Chai Point Corner", bank_account_name="CHAI POINT CORNER",
                          name_match=1.0)


def setup_chargeback(w: World) -> None:
    w.txns["T-2001"] = Txn(id="T-2001", payer_id="c_rohan", merchant_id="m_urban", amount=6800, bank="Gamma Bank", handle="@gamma",
                           debit="DEBITED", credit="CREDITED", status="SUCCESS", created_at=ago(24 * 9), order_ref="ORD-6602")
    w.disputes["D-7001"] = Dispute(id="D-7001", txn_id="T-2001", merchant_id="m_urban", amount=6800, reason="GOODS_NOT_RECEIVED",
                                   deadline=ahead(hours=36))
    w.order_evidence["T-2001"] = {
        "invoice": {"id": "INV-8841", "amount": 6800, "issued_at": ago(24 * 9).isoformat()},
        "delivery_proof": {"carrier": "SwiftShip", "awb": "SS99120044", "delivered_at": ago(24 * 6).isoformat(), "otp_verified": True},
        "customer_auth": {"device_match": True, "ip_city_match": True, "two_factor": True},
        "chat_log": {"messages": 4, "customer_confirmed_address": True},
    }


def setup_chargeback_no_evidence(w: World) -> None:
    w.txns["T-2002"] = Txn(id="T-2002", payer_id="c_zoya", merchant_id="m_chai", amount=2300, bank="Alpha Bank", handle="@alpha",
                           debit="DEBITED", credit="CREDITED", status="SUCCESS", created_at=ago(24 * 5), order_ref="ORD-6710")
    w.disputes["D-7002"] = Dispute(id="D-7002", txn_id="T-2002", merchant_id="m_chai", amount=2300, reason="SERVICE_NOT_AS_DESCRIBED",
                                   deadline=ahead(hours=20))
    w.order_evidence["T-2002"] = {"invoice": {"id": "INV-9003", "amount": 2300}}


def setup_emi(w: World) -> None:
    w.loans["L-9001"] = Loan(id="L-9001", party_id="c_priya", emi_amount=3200, due_date=ahead(days=1), status="DUE",
                             outstanding=41600, reschedules_last_12m=0, marketing_consent=False)


def setup_claim(w: World) -> None:
    w.claims["CL-3001"] = Claim(id="CL-3001", party_id="c_meera", kind="health", status="DOCS_PENDING",
                                missing_docs=["hospital_bill", "discharge_summary"])


def setup_soundbox(w: World) -> None:
    w.devices["SB-77"] = Device(id="SB-77", merchant_id="m_sharma", status="OFFLINE", last_seen=ago(26), firmware="1.3.9",
                                warranty_active=True, reboot_restores=True)


def setup_soundbox_dead(w: World) -> None:
    w.devices["SB-78"] = Device(id="SB-78", merchant_id="m_chai", status="OFFLINE", last_seen=ago(60), firmware="1.3.2",
                                warranty_active=True, reboot_restores=False)


def setup_novel(w: World) -> None:
    w.txns["T-3001"] = Txn(id="T-3001", payer_id="c_zoya", merchant_id="m_chai", amount=300, bank="Alpha Bank", handle="@alpha",
                           debit="DEBITED", credit="CREDITED", status="SUCCESS", created_at=ago(2))


def setup_scam(w: World) -> None:
    w.txns["T-3002"] = Txn(id="T-3002", payer_id="c_diya", merchant_id="m_rec", amount=4999, bank="Beta Bank", handle="@beta",
                           debit="DEBITED", credit="CREDITED", status="SUCCESS", created_at=ago(1))


def setup_refund_api_down(w: World) -> None:
    w.txns["T-1005"] = Txn(id="T-1005", payer_id="c_aarav", merchant_id="m_sharma", amount=899, bank="Alpha Bank", handle="@alpha",
                           debit="DEBITED", credit="FAILED", status="FAILED", created_at=ago(28), order_ref="ORD-5530")
    w.inject_failures["initiate_refund"] = 99  # persistent failure


def setup_refund_flaky(w: World) -> None:
    w.txns["T-1006"] = Txn(id="T-1006", payer_id="c_aarav", merchant_id="m_sharma", amount=640, bank="Alpha Bank", handle="@alpha",
                           debit="DEBITED", credit="FAILED", status="FAILED", created_at=ago(27), order_ref="ORD-5531")
    w.inject_failures["initiate_refund"] = 1  # transient: succeeds on retry


def setup_high_value_failed(w: World) -> None:
    w.txns["T-4001"] = Txn(id="T-4001", payer_id="c_rohan", merchant_id="m_nova", amount=32000, bank="Gamma Bank", handle="@gamma",
                           debit="DEBITED", credit="FAILED", status="FAILED", created_at=ago(31), order_ref="ORD-9001")


SETUPS = {
    "failed_payment_stuck": setup_failed_payment_stuck,
    "failed_payment_in_tat": setup_failed_payment_in_tat,
    "duplicate_debit": setup_duplicate_debit,
    "settlement_name_mismatch": setup_settlement_name_mismatch,
    "settlement_fraud_hold": setup_settlement_fraud_hold,
    "settlement_kyc_expired": setup_settlement_kyc_expired,
    "chargeback": setup_chargeback,
    "chargeback_no_evidence": setup_chargeback_no_evidence,
    "emi": setup_emi,
    "claim": setup_claim,
    "soundbox": setup_soundbox,
    "soundbox_dead": setup_soundbox_dead,
    "novel": setup_novel,
    "scam": setup_scam,
    "refund_api_down": setup_refund_api_down,
    "refund_flaky": setup_refund_flaky,
    "high_value_failed": setup_high_value_failed,
}


def build_world(only: Optional[list[str]] = None) -> World:
    w = World()
    base_parties(w)
    for key, fn in SETUPS.items():
        if only is None or key in only:
            fn(w)
    # Fixtures that inject failures must only be armed when their scenario runs, not at boot.
    w.inject_failures.clear()
    return w
