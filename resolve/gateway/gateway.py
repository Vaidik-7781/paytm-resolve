"""Typed tool gateway: the ONLY path from an agent to a system of record.

Defense in depth (independent of the policy engine and of every prompt):
  1. Strict JSON-schema validation of every call (pydantic)
  2. Write tools require a short-lived HMAC gate token minted by the orchestrator after the policy gate
  3. Hard value cap and per-case budgets enforced here as well
  4. Idempotency keys: replaying a call returns the first result, never a second side effect
  5. Dry run: every write tool can predict its state diff before commit
  6. Compensation: every write is recorded with its undo (saga-style rollback / one-tap undo)
  7. Full call trace for audit
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field, ValidationError

from ..mockworld.world import UpstreamError, World
from ..models import uid, utcnow


# ------------------------------------------------------------------ argument schemas
class _Args(BaseModel):
    model_config = {"extra": "forbid"}


class PartyArgs(_Args):
    party_id: str


class TxnArgs(_Args):
    txn_id: str


class RecentTxnArgs(_Args):
    party_id: str
    days: int = Field(default=7, ge=1, le=90)


class SettlementArgs(_Args):
    settlement_id: str


class MerchantArgs(_Args):
    merchant_id: str


class MerchantStatusArgs(_Args):
    merchant_id: str
    status: Optional[str] = None


class DisputeArgs(_Args):
    dispute_id: str


class LoanArgs(_Args):
    loan_id: str


class ClaimArgs(_Args):
    claim_id: str


class DeviceArgs(_Args):
    device_id: str


class SearchArgs(_Args):
    query: str
    k: int = Field(default=3, ge=1, le=8)


class RefundArgs(_Args):
    txn_id: str
    amount: float = Field(gt=0)
    reason: str = Field(max_length=200)


class RetryReversalArgs(_Args):
    txn_id: str


class ReleaseArgs(_Args):
    settlement_id: str


class RepresentmentArgs(_Args):
    dispute_id: str
    pack_id: str


class RescheduleArgs(_Args):
    loan_id: str
    new_due_date: str  # ISO date


class RequestDocsArgs(_Args):
    claim_id: str
    docs: list[str] = Field(min_length=1)


class NotifyArgs(_Args):
    party_id: str
    text: str = Field(max_length=1200)
    kind: str = "update"
    lang: str = "en"
    case_id: Optional[str] = None


class TicketArgs(_Args):
    case_id: Optional[str] = None
    queue: str
    priority: str = "P3"
    summary: str = Field(max_length=600)


# ------------------------------------------------------------------ results / budget
class ToolResult(BaseModel):
    ok: bool
    tool: str
    data: dict[str, Any] = Field(default_factory=dict)
    error_code: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = False
    dry_run: bool = False
    replayed: bool = False
    latency_ms: float = 0.0
    preview: dict[str, Any] = Field(default_factory=dict)
    ledger_id: Optional[str] = None


class CaseBudget:
    def __init__(self, max_calls: int = 40, max_write_value: float = 300_000) -> None:
        self.calls = 0
        self.write_value = 0.0
        self.max_calls = max_calls
        self.max_write_value = max_write_value


class ToolSpec:
    def __init__(self, name: str, scope: str, args: type[_Args], handler: Callable[[World, Any], dict],
                 description: str, dry: Optional[Callable[[World, Any], dict]] = None,
                 compensate: Optional[Callable[[World, Any, dict], dict]] = None,
                 value: Optional[Callable[[World, Any], float]] = None,
                 authz: Optional[Callable[[World, Any, str], bool]] = None) -> None:
        self.name, self.scope, self.args, self.handler = name, scope, args, handler
        self.description, self.dry, self.compensate, self.value, self.authz = description, dry, compensate, value, authz

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "scope": self.scope, "description": self.description, "input_schema": self.args.model_json_schema()}


def _dump(x: Any) -> dict[str, Any]:
    if x is None:
        raise UpstreamError("NOT_FOUND", retryable=False)
    if isinstance(x, list):
        return {"items": [i.model_dump(mode="json") for i in x]}
    return x.model_dump(mode="json")


def _build_tools(gw: "Gateway") -> dict[str, ToolSpec]:
    w = gw.world
    T: list[ToolSpec] = []
    # ----- reads
    T += [
        ToolSpec("get_party", "read", PartyArgs, lambda w, a: _dump(w.get_party(a.party_id)), "Party profile."),
        ToolSpec("list_recent_txns", "read", RecentTxnArgs, lambda w, a: _dump(w.list_recent_txns(a.party_id, a.days)), "Recent transactions of a party."),
        ToolSpec("get_txn", "read", TxnArgs, lambda w, a: _dump(w.get_txn(a.txn_id)), "Ledger record of a transaction."),
        ToolSpec("get_upi_status", "read", TxnArgs, lambda w, a: w.upi_status(a.txn_id), "UPI rail and bank leg status."),
        ToolSpec("get_settlement", "read", SettlementArgs, lambda w, a: _dump(w.get_settlement(a.settlement_id)), "Settlement record."),
        ToolSpec("list_settlements", "read", MerchantStatusArgs, lambda w, a: _dump(w.list_settlements(a.merchant_id, a.status)), "Settlements of a merchant."),
        ToolSpec("get_kyc", "read", MerchantArgs, lambda w, a: _dump(w.get_kyc(a.merchant_id)), "KYC and bank-name match record."),
        ToolSpec("get_dispute", "read", DisputeArgs, lambda w, a: _dump(w.get_dispute(a.dispute_id)), "Dispute record."),
        ToolSpec("list_disputes", "read", MerchantArgs, lambda w, a: _dump(w.list_disputes(a.merchant_id)), "Disputes of a merchant."),
        ToolSpec("get_order_evidence", "read", TxnArgs, lambda w, a: {"evidence": w.get_order_evidence(a.txn_id)}, "Order-system evidence for a transaction."),
        ToolSpec("get_loan", "read", LoanArgs, lambda w, a: _dump(w.get_loan(a.loan_id)), "Loan record."),
        ToolSpec("list_loans", "read", PartyArgs, lambda w, a: _dump(w.list_loans(a.party_id)), "Loans of a party."),
        ToolSpec("get_claim", "read", ClaimArgs, lambda w, a: _dump(w.get_claim(a.claim_id)), "Insurance claim."),
        ToolSpec("list_claims", "read", PartyArgs, lambda w, a: _dump(w.list_claims(a.party_id)), "Claims of a party."),
        ToolSpec("get_device", "read", DeviceArgs, lambda w, a: _dump(w.get_device(a.device_id)), "Soundbox device record."),
        ToolSpec("list_devices", "read", MerchantArgs, lambda w, a: _dump(w.list_devices(a.merchant_id)), "Devices of a merchant."),
        ToolSpec("search_policy", "read", SearchArgs, lambda w, a: {"hits": gw.kb.search(a.query, a.k) if gw.kb else []}, "Hybrid search over SOPs and regulatory rules."),
    ]

    # ----- writes (each with dry run + compensation)
    def dry_refund(w: World, a: RefundArgs) -> dict:
        t = w.get_txn(a.txn_id)
        return {"txn.refund_id": [t.refund_id if t else None, "RF-<new>"], "ledger": f"+{a.amount:,.0f} to payer's original source",
                "txn.reversal.status": [(t.reversal or {}).get("status") if t else None, "REFUNDED"]}

    def dry_release(w: World, a: ReleaseArgs) -> dict:
        s = w.get_settlement(a.settlement_id)
        return {"settlement.status": [s.status if s else None, "SETTLED"], "settlement.hold_reason": [s.hold_reason if s else None, None],
                "payout": f"{s.amount:,.0f} to verified bank account" if s else "n/a"}

    def dry_resched(w: World, a: RescheduleArgs) -> dict:
        l = w.get_loan(a.loan_id)
        return {"loan.due_date": [l.due_date.date().isoformat() if l else None, a.new_due_date[:10]], "loan.reschedules_last_12m": [l.reschedules_last_12m if l else None, (l.reschedules_last_12m + 1) if l else None]}

    T += [
        ToolSpec("initiate_refund", "write", RefundArgs, lambda w, a: w.refund(a.txn_id, a.amount, a.reason),
                 "Refund a failed/duplicate debit to the ORIGINAL source (destination cannot be set).", dry=dry_refund,
                 compensate=lambda w, a, r: w.cancel_refund(r["refund_id"]), value=lambda w, a: a.amount),
        ToolSpec("retry_reversal", "write", RetryReversalArgs, lambda w, a: w.retry_reversal(a.txn_id),
                 "Re-trigger the auto-reversal of a failed debit.",
                 dry=lambda w, a: {"txn.reversal.status": [((w.get_txn(a.txn_id).reversal or {}).get("status")), "INITIATED"]},
                 compensate=lambda w, a, r: {"note": "reversal retry is idempotent; nothing to undo"},
                 value=lambda w, a: (w.get_txn(a.txn_id).amount if w.get_txn(a.txn_id) else 0)),
        ToolSpec("trigger_reverification", "write", MerchantArgs, lambda w, a: w.trigger_reverification(a.merchant_id),
                 "Start merchant KYC/bank re-verification.",
                 dry=lambda w, a: {"kyc.reverification": [w.get_kyc(a.merchant_id).reverification if w.get_kyc(a.merchant_id) else None, "IN_PROGRESS"]},
                 compensate=lambda w, a, r: w.revert_verification(a.merchant_id), value=lambda w, a: 0),
        ToolSpec("validate_bank_account", "write", MerchantArgs, lambda w, a: w.validate_bank_account(a.merchant_id),
                 "Validate the merchant bank account (test-credit style) and name match.",
                 dry=lambda w, a: {"kyc.reverification": ["IN_PROGRESS", "PASSED if name_match >= 0.85 and no risk flags"]},
                 compensate=lambda w, a, r: w.revert_verification(a.merchant_id), value=lambda w, a: 0),
        ToolSpec("release_settlement_hold", "write", ReleaseArgs, lambda w, a: w.release_hold(a.settlement_id),
                 "Release a settlement hold after verification passed.", dry=dry_release,
                 compensate=lambda w, a, r: w.rehold(a.settlement_id, "BANK_NAME_MISMATCH"),
                 value=lambda w, a: (w.get_settlement(a.settlement_id).amount if w.get_settlement(a.settlement_id) else 0)),
        ToolSpec("file_representment", "write", RepresentmentArgs, lambda w, a: w.file_representment(a.dispute_id, a.pack_id),
                 "File the dispute representment with an evidence pack.",
                 dry=lambda w, a: {"dispute.status": [w.get_dispute(a.dispute_id).status if w.get_dispute(a.dispute_id) else None, "REPRESENTMENT_FILED"]},
                 compensate=lambda w, a, r: w.withdraw_representment(a.dispute_id),
                 value=lambda w, a: (w.get_dispute(a.dispute_id).amount if w.get_dispute(a.dispute_id) else 0)),
        ToolSpec("reschedule_emi", "write", RescheduleArgs,
                 lambda w, a: w.reschedule_emi(a.loan_id, datetime.fromisoformat(a.new_due_date).replace(tzinfo=utcnow().tzinfo)),
                 "Move an EMI due date once (consent required).", dry=dry_resched,
                 compensate=lambda w, a, r: w.restore_emi(a.loan_id, datetime.fromisoformat(r["old_due"])),
                 value=lambda w, a: (w.get_loan(a.loan_id).emi_amount if w.get_loan(a.loan_id) else 0)),
        ToolSpec("request_documents", "write", RequestDocsArgs, lambda w, a: w.request_documents(a.claim_id, a.docs),
                 "Ask the customer for missing claim documents.",
                 dry=lambda w, a: {"claim.requested_docs": [w.get_claim(a.claim_id).requested_docs if w.get_claim(a.claim_id) else [], a.docs]},
                 compensate=lambda w, a, r: w.cancel_document_request(a.claim_id, a.docs), value=lambda w, a: 0),
        ToolSpec("remote_reboot", "write", DeviceArgs, lambda w, a: w.reboot_device(a.device_id), "Remotely restart a Soundbox.",
                 dry=lambda w, a: {"device.status": [w.get_device(a.device_id).status if w.get_device(a.device_id) else None, "ACTIVE (if hardware healthy)"]},
                 compensate=lambda w, a, r: {"note": "restart is harmless; nothing to undo"}, value=lambda w, a: 0),
        ToolSpec("dispatch_replacement", "write", DeviceArgs, lambda w, a: w.dispatch_replacement(a.device_id), "Dispatch a replacement Soundbox (warranty).",
                 dry=lambda w, a: {"device.status": [w.get_device(a.device_id).status if w.get_device(a.device_id) else None, "REPLACEMENT_DISPATCHED"]},
                 compensate=lambda w, a, r: w.cancel_replacement(a.device_id), value=lambda w, a: 0),
        ToolSpec("notify_party", "write", NotifyArgs, lambda w, a: w.notify(a.party_id, a.text, a.kind, a.lang, a.case_id),
                 "Send an in-app notification/message to the party.", compensate=lambda w, a, r: {"note": "messages cannot be unsent"}),
        ToolSpec("create_ticket", "write", TicketArgs, lambda w, a: w.create_ticket(a.case_id, a.queue, a.priority, a.summary),
                 "Create a ticket for the human queue.", compensate=lambda w, a, r: {"note": "ticket left open for audit"}),
    ]
    # ----- object-level authorization: the actor may only touch records they own (system:* principals bypass)
    def own_txn(w: World, a: Any, actor: str) -> bool:
        t = w.get_txn(a.txn_id)
        return t is None or actor in (t.payer_id, t.merchant_id)

    A = {
        "get_party": lambda w, a, x: a.party_id == x, "list_recent_txns": lambda w, a, x: a.party_id == x,
        "get_txn": own_txn, "get_upi_status": own_txn, "get_order_evidence": own_txn,
        "get_settlement": lambda w, a, x: (w.get_settlement(a.settlement_id) is None) or w.get_settlement(a.settlement_id).merchant_id == x,
        "list_settlements": lambda w, a, x: a.merchant_id == x, "get_kyc": lambda w, a, x: a.merchant_id == x,
        "get_dispute": lambda w, a, x: (w.get_dispute(a.dispute_id) is None) or w.get_dispute(a.dispute_id).merchant_id == x,
        "list_disputes": lambda w, a, x: a.merchant_id == x,
        "get_loan": lambda w, a, x: (w.get_loan(a.loan_id) is None) or w.get_loan(a.loan_id).party_id == x, "list_loans": lambda w, a, x: a.party_id == x,
        "get_claim": lambda w, a, x: (w.get_claim(a.claim_id) is None) or w.get_claim(a.claim_id).party_id == x, "list_claims": lambda w, a, x: a.party_id == x,
        "get_device": lambda w, a, x: (w.get_device(a.device_id) is None) or w.get_device(a.device_id).merchant_id == x, "list_devices": lambda w, a, x: a.merchant_id == x,
        # writes: stricter, only the owner of the record may be acted for
        "initiate_refund": lambda w, a, x: (w.get_txn(a.txn_id) is None) or w.get_txn(a.txn_id).payer_id == x,
        "retry_reversal": own_txn,
        "trigger_reverification": lambda w, a, x: a.merchant_id == x, "validate_bank_account": lambda w, a, x: a.merchant_id == x,
        "release_settlement_hold": lambda w, a, x: (w.get_settlement(a.settlement_id) is None) or w.get_settlement(a.settlement_id).merchant_id == x,
        "file_representment": lambda w, a, x: (w.get_dispute(a.dispute_id) is None) or w.get_dispute(a.dispute_id).merchant_id == x,
        "reschedule_emi": lambda w, a, x: (w.get_loan(a.loan_id) is None) or w.get_loan(a.loan_id).party_id == x,
        "request_documents": lambda w, a, x: (w.get_claim(a.claim_id) is None) or w.get_claim(a.claim_id).party_id == x,
        "remote_reboot": lambda w, a, x: (w.get_device(a.device_id) is None) or w.get_device(a.device_id).merchant_id == x,
        "dispatch_replacement": lambda w, a, x: (w.get_device(a.device_id) is None) or w.get_device(a.device_id).merchant_id == x,
    }
    out = {t.name: t for t in T}
    for name, fn in A.items():
        out[name].authz = fn
    return out


class Gateway:
    def __init__(self, world: World, kb: Any = None, secret: str = "dev", hard_cap: float = 250_000) -> None:
        self.world = world
        self.kb = kb
        self._secret = secret.encode()
        self.hard_cap = hard_cap
        self.tools = _build_tools(self)
        self.idem: dict[str, ToolResult] = {}
        self.ledger: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------ gate tokens
    @staticmethod
    def _canon(args: dict[str, Any]) -> str:
        return json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))

    def issue_token(self, case_id: str, tool: str, args: dict[str, Any], tier: int, ttl_s: int = 180) -> str:
        exp = int(time.time()) + ttl_s
        msg = f"{case_id}|{tool}|{hashlib.sha256(self._canon(args).encode()).hexdigest()}|{tier}|{exp}"
        sig = hmac.new(self._secret, msg.encode(), hashlib.sha256).hexdigest()
        return f"{tier}.{exp}.{sig}"

    def _verify_token(self, token: Optional[str], case_id: str, tool: str, args: dict[str, Any]) -> tuple[bool, str]:
        if not token:
            return False, "MISSING_GATE_TOKEN"
        try:
            tier_s, exp_s, sig = token.split(".")
            tier, exp = int(tier_s), int(exp_s)
        except ValueError:
            return False, "MALFORMED_GATE_TOKEN"
        if exp < time.time():
            return False, "EXPIRED_GATE_TOKEN"
        if tier >= 3:
            return False, "TIER3_NEVER_EXECUTES"
        msg = f"{case_id}|{tool}|{hashlib.sha256(self._canon(args).encode()).hexdigest()}|{tier}|{exp}"
        good = hmac.new(self._secret, msg.encode(), hashlib.sha256).hexdigest()
        return (hmac.compare_digest(good, sig), "BAD_GATE_SIGNATURE")

    # ------------------------------------------------------------ calls
    def catalog(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    def call(self, case_id: str, tool: str, args: dict[str, Any], *, token: Optional[str] = None,
             idem_key: Optional[str] = None, dry_run: bool = False, budget: Optional[CaseBudget] = None,
             actor: Optional[str] = None) -> ToolResult:
        t0 = time.perf_counter()
        spec = self.tools.get(tool)
        if not spec:
            return self._fin(case_id, ToolResult(ok=False, tool=tool, error_code="UNKNOWN_TOOL", error=f"No such tool: {tool}"), t0, args)
        if budget:
            budget.calls += 1
            if budget.calls > budget.max_calls:
                return self._fin(case_id, ToolResult(ok=False, tool=tool, error_code="BUDGET_EXCEEDED", error="Per-case tool-call budget exceeded"), t0, args)
        try:
            parsed = spec.args(**args)
        except ValidationError as e:
            return self._fin(case_id, ToolResult(ok=False, tool=tool, error_code="INVALID_ARGS", error=str(e.errors()[0]["msg"])), t0, args)

        # object-level authorization (enforced here so even a compromised or over-eager agent cannot cross accounts)
        if actor is not None and not actor.startswith("system:") and spec.authz is not None and not spec.authz(self.world, parsed, actor):
            return self._fin(case_id, ToolResult(ok=False, tool=tool, error_code="ACCESS_DENIED", error="Record does not belong to the requester"), t0, args)

        if spec.scope == "read":
            try:
                return self._fin(case_id, ToolResult(ok=True, tool=tool, data=spec.handler(self.world, parsed)), t0, args)
            except UpstreamError as e:
                return self._fin(case_id, ToolResult(ok=False, tool=tool, error_code=e.code, retryable=e.retryable), t0, args)

        # ---- write path
        value = float(spec.value(self.world, parsed)) if spec.value else 0.0
        if value > self.hard_cap:
            return self._fin(case_id, ToolResult(ok=False, tool=tool, error_code="HARD_CAP_EXCEEDED", error=f"{value:,.0f} exceeds hard cap"), t0, args)
        if dry_run:
            prev = spec.dry(self.world, parsed) if spec.dry else {}
            return self._fin(case_id, ToolResult(ok=True, tool=tool, dry_run=True, preview=prev, data={"value": value}), t0, args)
        ok, why = self._verify_token(token, case_id, tool, args)
        if not ok:
            return self._fin(case_id, ToolResult(ok=False, tool=tool, error_code=why, error="Write blocked: no valid policy gate token"), t0, args)
        if budget and budget.write_value + value > budget.max_write_value:
            return self._fin(case_id, ToolResult(ok=False, tool=tool, error_code="WRITE_BUDGET_EXCEEDED", error="Per-case write value budget exceeded"), t0, args)
        key = idem_key or f"{case_id}:{tool}:{hashlib.sha1(self._canon(args).encode()).hexdigest()[:12]}"
        with self._lock:
            prior = self.idem.get(key)
            if prior is not None and prior.ok:
                rep = prior.model_copy(update={"replayed": True})
                return self._fin(case_id, rep, t0, args)
            try:
                data = spec.handler(self.world, parsed)
            except UpstreamError as e:
                return self._fin(case_id, ToolResult(ok=False, tool=tool, error_code=e.code, retryable=e.retryable), t0, args)
            lid = uid("led")
            res = ToolResult(ok=True, tool=tool, data=data, ledger_id=lid)
            self.idem[key] = res
            self.ledger[lid] = {"id": lid, "case_id": case_id, "tool": tool, "args": args, "result": data, "undone": False, "ts": utcnow().isoformat()}
        if budget:
            budget.write_value += value
        return self._fin(case_id, res, t0, args)

    def _fin(self, case_id: str, res: ToolResult, t0: float, args: dict[str, Any]) -> ToolResult:
        res.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        self.calls.append({"ts": utcnow().isoformat(), "case_id": case_id, "tool": res.tool, "ok": res.ok, "error": res.error_code,
                           "dry_run": res.dry_run, "replayed": res.replayed, "args": args})
        if len(self.calls) > 5000:
            del self.calls[:1000]
        return res

    # ------------------------------------------------------------ undo / saga compensation
    def undo(self, ledger_id: str) -> ToolResult:
        t0 = time.perf_counter()
        entry = self.ledger.get(ledger_id)
        if not entry:
            return ToolResult(ok=False, tool="undo", error_code="UNKNOWN_LEDGER_ENTRY")
        if entry["undone"]:
            return ToolResult(ok=False, tool="undo", error_code="ALREADY_UNDONE")
        spec = self.tools[entry["tool"]]
        if not spec.compensate:
            return ToolResult(ok=False, tool="undo", error_code="NOT_REVERSIBLE")
        try:
            parsed = spec.args(**entry["args"])
            out = spec.compensate(self.world, parsed, entry["result"])
        except UpstreamError as e:
            return ToolResult(ok=False, tool="undo", error_code=e.code)
        entry["undone"] = True
        # allow the same action to be re-issued later
        for k, v in list(self.idem.items()):
            if v.ledger_id == ledger_id:
                del self.idem[k]
        return self._fin(entry["case_id"], ToolResult(ok=True, tool="undo:" + entry["tool"], data=out), t0, entry["args"])

    def has_executed(self, case_or_any: Optional[str], tool: str, match: dict[str, Any]) -> bool:
        """Duplicate-action guard across cases: was this exact kind of write already executed (and not undone)?"""
        for e in self.ledger.values():
            if e["tool"] == tool and not e["undone"] and all(e["args"].get(k) == v for k, v in match.items()):
                return True
        return False
