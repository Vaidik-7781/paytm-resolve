"""Deterministic offline 'LLM'. Makes the whole system runnable with zero API keys (demo-safe on flaky Wi-Fi).

It implements the same interface as the Claude provider. Intent parsing is a quarantined, schema-bound
classifier: it only ever returns one enum value plus extracted ids; it never sees tools and never plans.
"""
from __future__ import annotations

import re
from typing import Any

from .i18n import render

INTENTS = ["payment_failed", "duplicate_debit", "settlement_held", "dispute", "emi_query", "claim_status", "soundbox", "unknown"]

KEYWORDS: dict[str, list[str]] = {
    "duplicate_debit": ["twice", "double", "two times", "duplicate", "dobara", "do baar", "2 baar", "दो बार", "दोबारा", "दुबारा"],
    "settlement_held": ["settlement", "payout", "settle nahi", "settle", "सेटलमेंट", "रुका", "ruka hua", "hold", "on hold", "paisa bank mein nahi", "bank mein nahi aaya", "बैंक में नहीं"],
    "dispute": ["chargeback", "dispute", "goods not received", "customer claims", "चार्जबैक", "विवाद"],
    "emi_query": ["emi", "loan", "kist", "किस्त", "ईएमआई", "due date", "installment", "instalment"],
    "claim_status": ["claim", "insurance", "क्लेम", "बीमा", "policy status"],
    "soundbox": ["soundbox", "sound box", "speaker", "awaaz", "आवाज़", "साउंडबॉक्स", "not speaking", "device offline"],
    "payment_failed": ["payment failed", "failed", "paisa kat", "paise kat", "kat gaya", "kat gayi", "deducted", "debited",
                       "not credited", "order fail", "transaction failed", "refund", "कट गए", "कट गया", "पैसे कट", "फेल", "वापस नहीं",
                       "money not received", "stuck", "pending", "wapas nahi"],
}
ID_PATTERNS = {
    "txn_id": r"\bT-\d{4}\b", "settlement_id": r"\bS-\d{4}\b", "dispute_id": r"\bD-\d{4}\b",
    "loan_id": r"\bL-\d{4}\b", "claim_id": r"\bCL-\d{4}\b", "device_id": r"\bSB-\d{2,4}\b",
}


def extract_entities(text: str) -> dict[str, Any]:
    ents: dict[str, Any] = {}
    for k, pat in ID_PATTERNS.items():
        m = re.findall(pat, text, flags=re.IGNORECASE)
        if m:
            ents[k] = m[0].upper()
    m = re.search(r"(?:rs\.?|₹|inr|rupees?)\s?([\d,]+(?:\.\d+)?)", text, flags=re.IGNORECASE)
    if m:
        try:
            ents["amount"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return ents


def classify(text: str) -> dict[str, Any]:
    t = text.lower()
    scores = {k: 0 for k in KEYWORDS}
    for intent, kws in KEYWORDS.items():
        for kw in kws:
            if kw in t:
                scores[intent] += 1
    # specific beats generic
    if scores["duplicate_debit"] and scores["payment_failed"]:
        scores["payment_failed"] = 0
    for specific in ("settlement_held", "dispute", "emi_query", "claim_status", "soundbox", "duplicate_debit"):
        if scores[specific] and scores["payment_failed"]:
            scores["payment_failed"] = max(0, scores["payment_failed"] - 1)
    best = max(scores, key=lambda k: scores[k])
    total = sum(scores.values())
    if scores[best] == 0:
        return {"intent": "unknown", "confidence": 0.35, "entities": extract_entities(text)}
    second = sorted(scores.values())[-2]
    conf = 0.72 + 0.1 * min(2, scores[best]) + (0.06 if scores[best] > second else -0.05)
    return {"intent": best, "confidence": round(min(0.97, conf), 2), "entities": extract_entities(text), "scores": {k: v for k, v in scores.items() if v}}


class OfflineLLM:
    name = "offline-deterministic"  # local trained intent model + rules, no API keys
    calls = 0

    async def classify_intent(self, masked_text: str, lang: str) -> dict[str, Any]:
        """Trained model + keyword rules; disagreement => unknown => human handoff (never a guess)."""
        self.calls += 1
        from ..ml.ensemble import combine
        return combine(masked_text)

    async def diagnose(self, brief: dict[str, Any]) -> dict[str, Any]:
        """No generative reasoning offline: report UNKNOWN so the case is escalated with a rich brief."""
        self.calls += 1
        return {"root_cause": "UNKNOWN", "confidence": 0.3, "reasoning": "No matching playbook; offline mode does not improvise.", "cited_docs": []}

    async def critique(self, plan: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {"agree": True, "concerns": []}

    async def compose(self, kind: str, lang: str, facts: dict[str, Any]) -> str:
        self.calls += 1
        return render(kind, lang, **facts)

    async def draft_evidence_narrative(self, dispute: dict[str, Any], evidence: dict[str, Any]) -> str:
        self.calls += 1
        parts = [f"Dispute {dispute['id']} ({dispute['reason'].replace('_', ' ').lower()}), amount {dispute['amount']:,.0f}."]
        inv = evidence.get("invoice")
        if inv:
            parts.append(f"Invoice {inv['id']} was issued for the same amount.")
        dp = evidence.get("delivery_proof")
        if dp:
            parts.append(f"Delivered via {dp['carrier']} (AWB {dp['awb']}); OTP verified by the customer at delivery." if dp.get("otp_verified")
                         else f"Delivered via {dp['carrier']} (AWB {dp['awb']}).")
        ca = evidence.get("customer_auth")
        if ca:
            parts.append("Customer authentication signals match: device " + ("matched" if ca.get("device_match") else "did not match") +
                         ", location " + ("matched" if ca.get("ip_city_match") else "did not match") + ".")
        if evidence.get("chat_log", {}).get("customer_confirmed_address"):
            parts.append("The customer confirmed the delivery address in the order chat.")
        parts.append("We request the chargeback be reversed in favour of the merchant.")
        return " ".join(parts)

    async def investigate(self, brief: dict[str, Any], tools: list[dict[str, Any]], call_tool, max_steps: int = 6) -> dict[str, Any]:
        """Deterministic read-only triage: gathers the same broad context an agentic LLM would, then summarises anomalies.
        Never improvises a root cause; it hands the human a head start."""
        self.calls += 1
        party, kind, text = brief.get("party_id"), brief.get("party_kind"), (brief.get("message") or "").lower()
        evidence, steps, recs = [], 0, []
        rt = await call_tool("list_recent_txns", {"party_id": party, "days": 14}); steps += 1
        items = rt.get("items", [])
        evidence.append(f"{len(items)} transaction(s) in the last 14 days")
        for t in items[:2]:
            st = await call_tool("get_upi_status", {"txn_id": t["id"]}); steps += 1
            evidence.append(f"{t['id']} {t['amount']:,.0f}: debit {st.get('debit_leg')}, credit {st.get('credit_leg')}, rail {st.get('rail_status')}")
            if st.get("debit_leg") == "DEBITED" and st.get("credit_leg") != "CREDITED":
                recs.append(f"Check reversal state of {t['id']}")
        if kind == "merchant":
            ss = await call_tool("list_settlements", {"merchant_id": party}); steps += 1
            held = [s for s in ss.get("items", []) if s["status"] == "HELD"]
            evidence.append(f"{len(held)} held settlement(s)")
            if held:
                recs.append("Review the settlement hold reason before replying")
        if re.search(r"\bqr\b|name|shop|different|spoof|fake", text):
            recs.append("Compare the merchant name registered for this QR/VPA with what the customer saw; escalate to merchant-ops/fraud if they differ")
        recs.append("Reply to the customer once the specialist has confirmed the finding")
        return {"hypothesis": "UNKNOWN", "confidence": 0.35, "reasoning": "No known pattern matched; deterministic triage collected recent activity for the specialist.",
                "recommended_human_actions": recs[:5], "evidence": evidence[:6], "steps": steps, "model": "offline-triage"}
