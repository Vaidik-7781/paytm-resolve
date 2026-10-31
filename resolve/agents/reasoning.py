"""Diagnosis, playbooks (root cause -> proposed actions) and the Critic agent.

Design: the *diagnosis* may use evidence, retrieval, episodic memory and (for novel cases) an LLM constrained to
an enum; the *actions* always come from reviewed playbooks, never free-form LLM output. The critic then re-checks
the plan against the evidence before anything reaches the policy gate.
"""
from __future__ import annotations

from typing import Any, Optional

from ..models import CriticVerdict, ProposedAction, RootCause
from .specialists import Findings

# code -> (category, base confidence, KB query, curated citations)
ROOT_CAUSES: dict[str, tuple[str, float, str, list[str]]] = {
    "REVERSAL_PENDING_WITHIN_TAT": ("payment_failed", 0.94, "failed payment debit reversal within TAT", ["SOP-PAY-01"]),
    "REVERSAL_STUCK_TAT_BREACHED": ("payment_failed", 0.93, "failed payment debit no reversal TAT breached refund", ["SOP-PAY-02", "SOP-PAY-01"]),
    "BANK_INCIDENT_AUTO_REVERSAL": ("payment_failed", 0.93, "issuer bank degradation incident reversal", ["SOP-PAY-04", "SOP-PAY-01"]),
    "PAYMENT_ACTUALLY_SUCCESSFUL": ("payment_failed", 0.95, "payment success merchant credited", ["SOP-PAY-01"]),
    "NO_DEBIT_OCCURRED": ("payment_failed", 0.95, "payment failed no debit", ["SOP-PAY-01"]),
    "ALREADY_REFUNDED": ("payment_failed", 0.97, "refund only once per transaction", ["SOP-PAY-02"]),
    "DUPLICATE_DEBIT_CONFIRMED": ("duplicate_debit", 0.94, "duplicate debit same order refund", ["SOP-PAY-03"]),
    "BANK_NAME_MISMATCH": ("settlement_held", 0.95, "settlement hold bank account name mismatch", ["SOP-SET-01"]),
    "KYC_EXPIRED": ("settlement_held", 0.94, "settlement hold expired KYC", ["SOP-SET-02"]),
    "RISK_FRAUD_REVIEW": ("settlement_held", 0.95, "settlement hold risk fraud review", ["SOP-SET-03"]),
    "SETTLEMENT_PROCESSING_NORMAL": ("settlement_held", 0.95, "settlement processing", ["SOP-SET-01"]),
    "CHARGEBACK_EVIDENCE_COMPLETE": ("dispute", 0.93, "chargeback representment checklist evidence", ["SOP-DSP-01"]),
    "CHARGEBACK_EVIDENCE_INCOMPLETE": ("dispute", 0.92, "chargeback incomplete evidence deadline", ["SOP-DSP-02"]),
    "EMI_DUE_RESCHEDULE_ELIGIBLE": ("emi_query", 0.92, "EMI reschedule eligibility consent", ["SOP-LND-01", "SOP-LND-02"]),
    "EMI_INFO_ONLY": ("emi_query", 0.95, "EMI due date information", ["SOP-LND-01"]),
    "CLAIM_DOCS_PENDING": ("claim_status", 0.94, "insurance claim documents pending", ["SOP-INS-01"]),
    "CLAIM_DOCS_ALREADY_REQUESTED": ("claim_status", 0.95, "insurance claim documents requested", ["SOP-INS-01"]),
    "CLAIM_IN_REVIEW": ("claim_status", 0.95, "insurance claim in review", ["SOP-INS-01"]),
    "DEVICE_OFFLINE_RECOVERABLE": ("soundbox", 0.92, "soundbox offline troubleshooting restart", ["SOP-DEV-01"]),
    "DEVICE_OUT_OF_WARRANTY": ("soundbox", 0.9, "soundbox out of warranty replacement approval", ["SOP-DEV-01"]),
    "DEVICE_ACTIVE": ("soundbox", 0.95, "soundbox online", ["SOP-DEV-01"]),
    "SCAM_SUSPECTED": ("unknown", 0.97, "scam social engineering OTP shared", ["SEC-SCAM-01"]),
    "ACCESS_DENIED_OTHER_ACCOUNT": ("unknown", 0.99, "access only to the requester's own records", ["SEC-AUTH-01"]),
    "UNKNOWN": ("unknown", 0.3, "escalation brief", ["GEN-ESC-01"]),
}
KNOWN = [c for c in ROOT_CAUSES if c != "UNKNOWN"]


def _pick(intent: str, fs: list[Findings], ctx) -> tuple[str, dict[str, Any], str]:
    """Rule-based root cause. Returns (code, signals, rationale)."""
    f = next((x for x in fs if x.signals), fs[0] if fs else None)
    if f is None:
        return "UNKNOWN", {}, "No specialist produced findings."
    s = f.signals
    if f.access_denied:
        return "ACCESS_DENIED_OTHER_ACCOUNT", s, "The referenced record is not owned by the requester."

    if intent in ("payment_failed", "duplicate_debit"):
        if not s.get("txn_id"):
            return "UNKNOWN", s, "No matching transaction found for the requester."
        if s.get("duplicate_of"):
            return "DUPLICATE_DEBIT_CONFIRMED", s, f"{s['txn_id']} duplicates {s['duplicate_of']}: same payer, merchant and amount within minutes; only the original was credited."
        if s.get("refund_id"):
            return "ALREADY_REFUNDED", s, f"A refund ({s['refund_id']}) already exists for {s['txn_id']}; no second refund."
        if s["debit"] == "NOT_DEBITED":
            return "NO_DEBIT_OCCURRED", s, "Debit leg did not happen; nothing to reverse."
        if s["credit"] == "CREDITED":
            return "PAYMENT_ACTUALLY_SUCCESSFUL", s, "Credit leg succeeded; the merchant has been paid."
        if s.get("incident") or s.get("bank_state") == "DEGRADED":
            return "BANK_INCIDENT_AUTO_REVERSAL", s, f"Part of open incident on {s['bank']}; automatic reversal covers it."
        tat = s["tat_hours"]
        if s["age_hours"] > tat:
            return "REVERSAL_STUCK_TAT_BREACHED", s, f"Debit succeeded, credit {s['credit']}, and the {tat}h reversal window has passed (age {s['age_hours']:.0f}h)."
        return "REVERSAL_PENDING_WITHIN_TAT", s, f"Debit succeeded, credit {s['credit']}; reversal is {s.get('reversal_status') or 'expected'} and still inside the {tat}h window."

    if intent == "settlement_held":
        if not s.get("settlement_id"):
            return "UNKNOWN", s, "No settlement found."
        if s["status"] != "HELD":
            return "SETTLEMENT_PROCESSING_NORMAL", s, f"Settlement is {s['status']}, not held."
        if s.get("risk_flags") or s.get("hold_reason") == "RISK_FRAUD_REVIEW":
            return "RISK_FRAUD_REVIEW", s, f"Risk flags present on the merchant: {s.get('risk_flags')}."
        if s.get("hold_reason") == "KYC_EXPIRED":
            return "KYC_EXPIRED", s, "KYC has expired; agent cannot release."
        if s.get("hold_reason") == "BANK_NAME_MISMATCH":
            return "BANK_NAME_MISMATCH", s, f"Bank name '{s.get('bank_account_name')}' vs KYC '{s.get('legal_name')}', fuzzy match {s.get('name_match')}."
        return "UNKNOWN", s, f"Hold reason '{s.get('hold_reason')}' has no playbook."

    if intent == "dispute":
        if not s.get("dispute_id"):
            return "UNKNOWN", s, "No open dispute."
        if not s["missing"]:
            return "CHARGEBACK_EVIDENCE_COMPLETE", s, f"All {len(s['required'])} required exhibits present with {s['hours_left']}h left."
        return "CHARGEBACK_EVIDENCE_INCOMPLETE", s, f"Missing exhibits: {s['missing']}; {s['hours_left']}h left."

    if intent == "emi_query":
        if not s.get("loan_id"):
            return "UNKNOWN", s, "No loan found."
        if s["hardship_signal"] and s["eligible"]:
            return "EMI_DUE_RESCHEDULE_ELIGIBLE", s, "Customer signals difficulty and is eligible for a one-time reschedule (consent still required)."
        return "EMI_INFO_ONLY", s, "Information request, or not eligible for reschedule."

    if intent == "claim_status":
        if not s.get("claim_id"):
            return "UNKNOWN", s, "No claim found."
        if s["still_to_request"]:
            return "CLAIM_DOCS_PENDING", s, f"Claim waiting for {s['still_to_request']}."
        if s["status"] == "DOCS_PENDING":
            return "CLAIM_DOCS_ALREADY_REQUESTED", s, "Documents were already requested; avoid duplicate requests."
        return "CLAIM_IN_REVIEW", s, f"Claim is {s['status']}."

    if intent == "soundbox":
        if not s.get("device_id"):
            return "UNKNOWN", s, "No device found."
        if s["status"] == "ACTIVE":
            return "DEVICE_ACTIVE", s, "Device is online."
        if s["warranty"]:
            return "DEVICE_OFFLINE_RECOVERABLE", s, f"Offline {s['offline_hours']}h, firmware {s['firmware']}: try remote restart, then warranty replacement."
        return "DEVICE_OUT_OF_WARRANTY", s, "Offline and out of warranty: replacement is paid and needs approval."
    return "UNKNOWN", s, "No playbook for this intent."


async def diagnose(ctx, intent: str, fs: list[Findings]) -> RootCause:
    code, sig, why = _pick(intent, fs, ctx)
    cat, base, query, cites = ROOT_CAUSES[code]
    conf = base
    # name match too low to self-verify
    if code == "BANK_NAME_MISMATCH" and (sig.get("name_match") or 0) < 0.85:
        conf = 0.65
        why += " Match below 0.85: cannot self-verify."
    source = "rules"
    if code == "UNKNOWN":
        from .investigator import investigate
        f = await investigate(ctx, intent)
        inv = f.signals["investigation"]
        fs.append(f)
        why += f" AI hypothesis (never executed, for the human): {inv['hypothesis']} at {inv['confidence']:.2f}. {inv['reasoning']}"
        conf = min(conf, 0.45)  # generative reasoning can inform a human, never lift trust
        source = "investigator"
        sig = {**sig, "hypothesis": inv["hypothesis"], "hypothesis_confidence": inv["confidence"]}
    feats = ctx.engine.memory.features(intent, code, sig.get("bank", "-"), float(sig.get("amount") or 0))
    conf2, note = ctx.engine.memory.calibrate(conf, feats)
    ic = ctx.facts.get("intent_conf")
    if ic is not None and code != "UNKNOWN":  # a shaky understanding of the message must lower trust in everything downstream
        cap = 0.55 + 0.45 * ic
        if conf2 > cap:
            note += f"; capped at {cap:.2f} by intent confidence {ic:.2f}"
            conf2 = cap
    ctx.facts["calibration_note"] = note
    ctx.facts["features"] = feats
    hits = ctx.engine.kb.search(query, 3)
    ctx.facts["kb_hits"] = hits
    cites = list(dict.fromkeys(cites + [hits[0]["id"]]))[:3] if hits else cites
    return RootCause(code=code, confidence=round(conf2, 3), rationale=why, category=cat, citations=cites, signals={**sig, "source": source})


# ============================================================================= playbooks
def plan_actions(ctx, rc: RootCause, fs: list[Findings]) -> list[ProposedAction]:
    s = rc.signals
    case = ctx.case
    acts: list[ProposedAction] = []
    merchant_party = case.party_kind == "merchant"

    def ticket(queue: str, priority: str, summary: str) -> ProposedAction:
        return ProposedAction(tool="create_ticket", kind="escalate", label=f"Escalate to {queue}",
                              args={"case_id": case.id, "queue": queue, "priority": priority, "summary": summary[:550]},
                              expected_effect=f"Ticket opened in {queue} with the full brief")

    code = rc.code
    if code in ("REVERSAL_STUCK_TAT_BREACHED", "DUPLICATE_DEBIT_CONFIRMED") and not merchant_party:
        acts.append(ProposedAction(tool="initiate_refund", label=f"Refund {s['amount']:,.0f} to original source",
                                   args={"txn_id": s["txn_id"], "amount": float(s["amount"]), "reason": code}, value=float(s["amount"]),
                                   expected_effect=f"Refund of {s['amount']:,.0f} to the payer's original source for {s['txn_id']}"))
    elif code == "BANK_NAME_MISMATCH" and rc.confidence >= 0.8:
        m, sid = s["merchant_id"], s["settlement_id"]
        acts += [
            ProposedAction(tool="trigger_reverification", label="Start bank/KYC re-verification", args={"merchant_id": m}, expected_effect="Re-verification in progress"),
            ProposedAction(tool="validate_bank_account", label="Validate bank account (test credit + name match)", args={"merchant_id": m},
                           precondition="trigger_reverification.state", expected_effect="Name match confirmed"),
            ProposedAction(tool="release_settlement_hold", label=f"Release hold on {sid}", args={"settlement_id": sid}, value=float(s["amount"]),
                           precondition="validate_bank_account.passed", expected_effect=f"{s['amount']:,.0f} released to the verified bank account"),
        ]
    elif code == "BANK_NAME_MISMATCH":
        acts.append(ticket("merchant-ops", "P2", f"Name mismatch below auto-verify threshold ({s.get('name_match')}) on {s['settlement_id']}"))
    elif code == "KYC_EXPIRED":
        acts.append(ProposedAction(tool="trigger_reverification", label="Start KYC refresh with the merchant", args={"merchant_id": s["merchant_id"]},
                                   expected_effect="Merchant receives the KYC refresh request"))
    elif code == "RISK_FRAUD_REVIEW":
        acts.append(ticket("risk-ops", "P1", f"Settlement {s['settlement_id']} ({s['amount']:,.0f}) on risk hold; flags {s.get('risk_flags')}. Do not release without risk sign-off."))
    elif code == "CHARGEBACK_EVIDENCE_COMPLETE":
        pack = next((f.evidence.get("pack") for f in fs if f.evidence.get("pack")), None)
        if pack:
            acts.append(ProposedAction(tool="file_representment", label=f"File representment for {s['dispute_id']}",
                                       args={"dispute_id": s["dispute_id"], "pack_id": pack["pack_id"]}, value=float(s["amount"]),
                                       expected_effect="Dispute answered with the evidence pack before the deadline"))
    elif code == "CHARGEBACK_EVIDENCE_INCOMPLETE" and (s.get("hours_left") or 99) < 24:
        acts.append(ticket("disputes-ops", "P1", f"{s['dispute_id']} due in {s['hours_left']}h with missing exhibits {s['missing']}"))
    elif code == "EMI_DUE_RESCHEDULE_ELIGIBLE":
        acts.append(ProposedAction(tool="reschedule_emi", kind="sales", label=f"Move EMI to {s['new_due_date']} (one time)",
                                   args={"loan_id": s["loan_id"], "new_due_date": s["new_due_date"]}, value=float(s["emi"]),
                                   expected_effect=f"EMI due date moves to {s['new_due_date']}"))
    elif code == "CLAIM_DOCS_PENDING":
        acts.append(ProposedAction(tool="request_documents", label="Request missing claim documents", args={"claim_id": s["claim_id"], "docs": s["still_to_request"]},
                                   expected_effect=f"Customer asked for {', '.join(s['still_to_request'])}"))
    elif code == "DEVICE_OFFLINE_RECOVERABLE":
        acts += [
            ProposedAction(tool="remote_reboot", label="Restart Soundbox remotely", args={"device_id": s["device_id"]}, expected_effect="Device comes back online"),
            ProposedAction(tool="dispatch_replacement", label="Dispatch warranty replacement if restart fails", args={"device_id": s["device_id"]},
                           precondition="!remote_reboot.restored", expected_effect="Replacement shipped under warranty"),
        ]
    elif code == "DEVICE_OUT_OF_WARRANTY":
        acts.append(ticket("devices-ops", "P3", f"{s['device_id']} offline {s['offline_hours']}h and out of warranty: paid replacement approval"))
    elif code == "SCAM_SUSPECTED":
        acts.append(ticket("fraud-ops", "P1", "Customer reports sharing an OTP/screen with a caller. Probable social-engineering scam; contact customer and review recent debits."))
    elif code == "ACCESS_DENIED_OTHER_ACCOUNT":
        acts.append(ticket("security", "P3", "Requester asked about a record that is not theirs; access refused and logged."))
    elif code == "UNKNOWN":
        acts.append(ticket("ops-triage", "P2", "No playbook matched. Specialist triage needed; see brief."))
    if "injection_attempt" in case.flags:
        sec = ticket("security", "P3", "Prompt-injection attempt detected in customer message; injected instruction ignored, genuine issue handled.")
        sec.kind = "notify"
        sec.label = "Open security ticket (side effect)"
        acts.append(sec)
    return acts


# ============================================================================= critic
async def critique(ctx, rc: RootCause, acts: list[ProposedAction], fs: list[Findings]) -> CriticVerdict:
    case, s = ctx.case, rc.signals
    checks: list[dict[str, Any]] = []
    issues: list[str] = []

    def add(name: str, ok: bool, detail: str, fatal: bool = False) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            issues.append(f"{name}: {detail}" + (" [fatal]" if fatal else ""))

    # C1 authorization: every record touched belongs to the requester
    owners: dict[tuple[str, str], set[str]] = {}
    for f in fs:
        for kind, rid, owner in f.refs:
            owners.setdefault((kind, rid), set()).add(owner)
    bad = [f"{k}:{i}" for (k, i), o in owners.items() if case.party_id not in o]
    add("authorization", not bad and not any(f.access_denied for f in fs) or rc.code == "ACCESS_DENIED_OTHER_ACCOUNT",
        "all referenced records belong to the requester" if not bad else f"not owned by requester: {bad}", fatal=bool(bad))

    # C2 evidence supports each state-changing action
    support_ok, support_detail = True, "every write is backed by evidence"
    for a in acts:
        t = a.tool
        if t == "initiate_refund":
            good = s.get("debit") == "DEBITED" and s.get("credit") != "CREDITED" and not s.get("refund_id") and a.args["amount"] <= s.get("amount", 0) + 1e-9
        elif t in ("release_settlement_hold", "validate_bank_account"):
            good = s.get("hold_reason") == "BANK_NAME_MISMATCH" and (s.get("name_match") or 0) >= 0.85 and not s.get("risk_flags")
        elif t == "file_representment":
            good = bool(s.get("completeness") == 1.0 and (s.get("hours_left") or 0) > 0)
        elif t == "reschedule_emi":
            good = bool(s.get("eligible") and s.get("hardship_signal"))
        elif t == "request_documents":
            good = set(a.args["docs"]) <= set(s.get("missing_docs", []))
        elif t in ("remote_reboot", "dispatch_replacement"):
            good = s.get("status") == "OFFLINE"
        elif t == "trigger_reverification":
            good = s.get("hold_reason") in ("BANK_NAME_MISMATCH", "KYC_EXPIRED")
        else:
            good = True
        if not good:
            support_ok, support_detail = False, f"{t} is not supported by the evidence"
    add("evidence_support", support_ok, support_detail, fatal=not support_ok)

    # C3 no smuggled destinations: arguments must come from systems of record, not the customer message
    smuggled = [v for a in acts for v in a.args.values() if isinstance(v, str) and v in ctx.guard.injected_targets]
    add("no_injected_parameters", not smuggled, "no account/VPA from the message is used" if not smuggled else f"message-supplied target found: {smuggled}", fatal=bool(smuggled))

    # C4 citation present
    add("policy_citation", bool(rc.citations), f"grounded in {', '.join(rc.citations)}" if rc.citations else "no SOP cited")

    # C5 amount bounds
    over = [a.tool for a in acts if a.tool == "initiate_refund" and a.args["amount"] > s.get("amount", 0) + 1e-9]
    add("amount_bound", not over, "refund never exceeds the debited amount" if not over else "refund exceeds debited amount", fatal=bool(over))

    # C6 independent LLM second opinion (no-op offline)
    second = None
    if acts and ctx.engine.llm.name != "offline-deterministic":
        out = await ctx.engine.llm.critique({"root_cause": rc.code, "rationale": rc.rationale, "signals": s, "actions": [a.model_dump(mode="json") for a in acts]})
        case.llm_calls += 1
        second = "agrees" if out["agree"] else "; ".join(out["concerns"]) or "disagrees"
        add("llm_second_opinion", out["agree"], second)

    fatal = any((not c["ok"]) and c["name"] in ("authorization", "evidence_support", "no_injected_parameters", "amount_bound") for c in checks)
    if fatal and rc.code != "ACCESS_DENIED_OTHER_ACCOUNT":
        verdict, delta = "reject", -0.4
    elif issues:
        verdict, delta = "revise", -0.1
    else:
        verdict, delta = "approve", 0.0
    return CriticVerdict(verdict=verdict, checks=checks, issues=issues, confidence_delta=delta, llm_second_opinion=second)
