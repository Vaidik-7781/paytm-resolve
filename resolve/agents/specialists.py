"""Specialist agents. Each one is READ-ONLY: it investigates through the gateway and returns structured Findings.
No specialist can change state; actions are proposed later from playbooks and must pass critic, policy gate and gateway.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from ..models import utcnow

HARDSHIP = re.compile(r"(kam hain|kam hai|tight|short on|can'?t pay|cannot pay|mushkil|dikkat|nahi hai paise|paise nahi|extend|aage kar|postpone|delay|कम हैं|मुश्किल|दिक्कत|आगे)", re.I)


@dataclass
class Findings:
    agent: str
    summary: str
    signals: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    refs: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, id, owner_party_id)
    access_denied: bool = False


def _parse_dt(x: Any) -> Optional[datetime]:
    if isinstance(x, datetime):
        return x
    try:
        return datetime.fromisoformat(str(x))
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------- Transaction tracer
async def transaction_tracer(ctx, ent: dict[str, Any], want_duplicate: bool = False) -> Findings:
    A = "Transaction tracer"
    party = ctx.case.party_id
    txn: Optional[dict[str, Any]] = None
    if ent.get("txn_id"):
        r = await ctx.read(A, "get_txn", {"txn_id": ent["txn_id"]}, owner_of=lambda d: [d.get("payer_id"), d.get("merchant_id")])
        if r.error_code == "ACCESS_DENIED":
            return Findings(A, f"{ent['txn_id']} does not belong to the requester; details withheld.", {"access_denied": True}, access_denied=True)
        if r.ok:
            txn = r.data
    if txn is None:
        r = await ctx.read(A, "list_recent_txns", {"party_id": party, "days": 7})
        items = r.data.get("items", []) if r.ok else []
        if want_duplicate:
            pair = _find_duplicate(items)
            if pair:
                txn = pair["duplicate"]
                ctx.facts["duplicate_of"] = pair["original"]["id"]
        if txn is None:
            cand = [t for t in items if t["debit"] == "DEBITED" and t["credit"] != "CREDITED"] or items
            txn = cand[0] if cand else None
    if txn is None:
        return Findings(A, "No recent transaction found for this party.", {"txn_found": False})
    upi = await ctx.read(A, "get_upi_status", {"txn_id": txn["id"]})
    s = upi.data if upi.ok else {}
    inc = next((i for i in ctx.engine.world.incidents.values() if i.status == "OPEN" and i.cluster == txn["bank"] and txn["id"] in i.affected_txns), None)
    tat = ctx.engine.policy.policy["reversal_tat_hours"]
    rev = txn.get("reversal") or {}
    age = float(s.get("age_hours", 0))
    eta_h = None
    if rev.get("eta_at"):
        eta = _parse_dt(rev["eta_at"])
        if eta:
            eta_h = max(1, round((eta - utcnow()).total_seconds() / 3600))
    sig = {
        "txn_id": txn["id"], "amount": txn["amount"], "bank": txn["bank"], "debit": txn["debit"], "credit": txn["credit"], "age_hours": age,
        "reversal_status": rev.get("status"), "refund_id": txn.get("refund_id"), "tat_hours": tat, "eta_hours": eta_h or max(1, round(tat - age)),
        "bank_state": s.get("bank_state", "OK"), "incident": inc.id if inc else None, "payer_id": txn["payer_id"], "merchant_id": txn["merchant_id"],
        "duplicate_of": ctx.facts.get("duplicate_of"), "response_code": s.get("response_code"),
    }
    summ = (f"{txn['id']} {txn['amount']:,.0f} via {txn['bank']}: debit {txn['debit']}, credit {txn['credit']}, "
            f"reversal {rev.get('status') or 'none'}, age {age:.0f}h" + (f", part of incident {inc.id}" if inc else ""))
    return Findings(A, summ, sig, {"upi": s}, refs=[("txn", txn["id"], txn["payer_id"]), ("txn", txn["id"], txn["merchant_id"])])


def _find_duplicate(items: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for a in items:
        for b in items:
            if a["id"] == b["id"] or a["merchant_id"] != b["merchant_id"] or abs(a["amount"] - b["amount"]) > 1e-6:
                continue
            ta, tb = _parse_dt(a["created_at"]), _parse_dt(b["created_at"])
            if ta and tb and abs((ta - tb).total_seconds()) <= 300 and a["credit"] == "CREDITED" and b["credit"] != "CREDITED" and b["debit"] == "DEBITED":
                return {"original": a, "duplicate": b}
    return None


# ----------------------------------------------------------------------------- Settlement analyst
async def settlement_analyst(ctx, ent: dict[str, Any]) -> Findings:
    A = "Settlement analyst"
    party = ctx.case.party_id
    s: Optional[dict[str, Any]] = None
    if ent.get("settlement_id"):
        r = await ctx.read(A, "get_settlement", {"settlement_id": ent["settlement_id"]}, owner_of=lambda d: [d.get("merchant_id")])
        if r.error_code == "ACCESS_DENIED":
            return Findings(A, "Settlement belongs to another merchant; details withheld.", {"access_denied": True}, access_denied=True)
        if r.ok:
            s = r.data
    if s is None:
        r = await ctx.read(A, "list_settlements", {"merchant_id": party})
        items = r.data.get("items", []) if r.ok else []
        held = [i for i in items if i["status"] == "HELD"]
        s = (held or items or [None])[0]
    if s is None:
        return Findings(A, "No settlement found.", {"settlement_found": False})
    k = await ctx.read(A, "get_kyc", {"merchant_id": s["merchant_id"]})
    kyc = k.data if k.ok else {}
    sig = {
        "settlement_id": s["id"], "amount": s["amount"], "status": s["status"], "hold_reason": s.get("hold_reason"),
        "kyc_status": kyc.get("status"), "name_match": kyc.get("name_match"), "risk_flags": kyc.get("risk_flags", []),
        "legal_name": kyc.get("legal_name"), "bank_account_name": kyc.get("bank_account_name"), "reverification": kyc.get("reverification"),
        "merchant_id": s["merchant_id"],
    }
    summ = (f"{s['id']} {s['amount']:,.0f} is {s['status']}" + (f" ({s['hold_reason']})" if s.get("hold_reason") else "") +
            (f"; KYC name '{kyc.get('legal_name')}' vs bank '{kyc.get('bank_account_name')}' match {kyc.get('name_match')}" if kyc else ""))
    return Findings(A, summ, sig, {"kyc": kyc}, refs=[("settlement", s["id"], s["merchant_id"])])


# ----------------------------------------------------------------------------- Dispute builder (generative evidence pack)
REQUIRED = {
    "GOODS_NOT_RECEIVED": ["invoice", "delivery_proof", "customer_auth", "chat_log"],
    "SERVICE_NOT_AS_DESCRIBED": ["invoice", "chat_log", "service_proof"],
}


async def dispute_builder(ctx, ent: dict[str, Any]) -> Findings:
    A = "Dispute builder"
    party = ctx.case.party_id
    d: Optional[dict[str, Any]] = None
    if ent.get("dispute_id"):
        r = await ctx.read(A, "get_dispute", {"dispute_id": ent["dispute_id"]}, owner_of=lambda x: [x.get("merchant_id")])
        if r.error_code == "ACCESS_DENIED":
            return Findings(A, "Dispute belongs to another merchant; details withheld.", {"access_denied": True}, access_denied=True)
        d = r.data if r.ok else None
    if d is None:
        r = await ctx.read(A, "list_disputes", {"merchant_id": party})
        opens = sorted([x for x in r.data.get("items", []) if x["status"] == "OPEN"], key=lambda x: x["deadline"]) if r.ok else []
        d = opens[0] if opens else None
    if d is None:
        return Findings(A, "No open dispute found.", {"dispute_found": False})
    ev = await ctx.read(A, "get_order_evidence", {"txn_id": d["txn_id"]})
    evidence = ev.data.get("evidence", {}) if ev.ok else {}
    req = REQUIRED.get(d["reason"], ["invoice", "chat_log"])
    present = [x for x in req if x in evidence and (x != "delivery_proof" or evidence[x].get("otp_verified") is not None)]
    missing = [x for x in req if x not in present]
    deadline = _parse_dt(d["deadline"])
    hours_left = round((deadline - utcnow()).total_seconds() / 3600, 1) if deadline else None
    pack = None
    if not missing:
        narrative = await ctx.engine.llm.draft_evidence_narrative(d, evidence)
        ctx.case.llm_calls += 1
        pack = {"pack_id": f"PK-{d['id']}", "dispute_id": d["id"], "exhibits": present, "narrative": narrative, "completeness": 1.0}
        await ctx.emit(A, "generate", "Evidence pack generated", narrative, {"pack": pack})
    sig = {"dispute_id": d["id"], "txn_id": d["txn_id"], "amount": d["amount"], "reason": d["reason"], "hours_left": hours_left,
           "required": req, "present": present, "missing": missing, "completeness": round(len(present) / len(req), 2), "merchant_id": d["merchant_id"],
           "deadline": d["deadline"]}
    return Findings(A, f"{d['id']} {d['amount']:,.0f} ({d['reason']}), {hours_left}h left; evidence {len(present)}/{len(req)}" +
                    (f", missing {', '.join(missing)}" if missing else ""), sig, {"pack": pack, "evidence": evidence}, refs=[("dispute", d["id"], d["merchant_id"])])


# ----------------------------------------------------------------------------- Lending agent
async def lending_agent(ctx, ent: dict[str, Any]) -> Findings:
    A = "Lending agent"
    r = await ctx.read(A, "list_loans", {"party_id": ctx.case.party_id})
    loans = r.data.get("items", []) if r.ok else []
    if ent.get("loan_id"):
        loans = [l for l in loans if l["id"] == ent["loan_id"]] or loans
    if not loans:
        return Findings(A, "No active loan found for this party.", {"loan_found": False})
    l = loans[0]
    due = _parse_dt(l["due_date"])
    days = round((due - utcnow()).total_seconds() / 86400, 1) if due else None
    eligible = l["status"] != "OVERDUE" and l["reschedules_last_12m"] < 1
    hardship = bool(HARDSHIP.search(ctx.case.message or ""))
    new_due = (due + timedelta(days=7)).date().isoformat() if due else None
    sig = {"loan_id": l["id"], "emi": l["emi_amount"], "due_date": (due.date().isoformat() if due else None), "days_to_due": days, "status": l["status"],
           "outstanding": l["outstanding"], "eligible": eligible, "hardship_signal": hardship, "new_due_date": new_due, "party_id": l["party_id"]}
    return Findings(A, f"{l['id']} EMI {l['emi_amount']:,.0f} due in {days}d, {l['status']}; reschedule eligible={eligible}; hardship signal={hardship}", sig,
                    refs=[("loan", l["id"], l["party_id"])])


# ----------------------------------------------------------------------------- Claims agent
async def claims_agent(ctx, ent: dict[str, Any]) -> Findings:
    A = "Claims agent"
    r = await ctx.read(A, "list_claims", {"party_id": ctx.case.party_id})
    items = r.data.get("items", []) if r.ok else []
    if ent.get("claim_id"):
        items = [c for c in items if c["id"] == ent["claim_id"]] or items
    if not items:
        return Findings(A, "No claim found.", {"claim_found": False})
    c = items[0]
    still_missing = [d for d in c["missing_docs"] if d not in c["requested_docs"]]
    sig = {"claim_id": c["id"], "status": c["status"], "missing_docs": c["missing_docs"], "requested_docs": c["requested_docs"], "still_to_request": still_missing,
           "eta_days": c["eta_days"], "party_id": c["party_id"]}
    return Findings(A, f"{c['id']} is {c['status']}; missing {c['missing_docs']}, already requested {c['requested_docs']}", sig, refs=[("claim", c["id"], c["party_id"])])


# ----------------------------------------------------------------------------- Device agent
async def device_agent(ctx, ent: dict[str, Any]) -> Findings:
    A = "Device agent"
    r = await ctx.read(A, "list_devices", {"merchant_id": ctx.case.party_id})
    items = r.data.get("items", []) if r.ok else []
    if ent.get("device_id"):
        items = [d for d in items if d["id"] == ent["device_id"]] or items
    if not items:
        return Findings(A, "No device found for this merchant.", {"device_found": False})
    d = next((x for x in items if x["status"] == "OFFLINE"), items[0])
    seen = _parse_dt(d["last_seen"])
    off_h = round((utcnow() - seen).total_seconds() / 3600, 1) if seen else None
    sig = {"device_id": d["id"], "status": d["status"], "offline_hours": off_h, "firmware": d["firmware"], "warranty": d["warranty_active"], "merchant_id": d["merchant_id"]}
    return Findings(A, f"{d['id']} is {d['status']} for {off_h}h, firmware {d['firmware']}, warranty={d['warranty_active']}", sig, refs=[("device", d["id"], d["merchant_id"])])
