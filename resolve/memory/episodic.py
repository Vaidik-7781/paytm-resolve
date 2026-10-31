"""Episodic case memory: resolved cases are stored as feature sets and retrieved by Jaccard similarity.

Used for (a) calibrating confidence (similar cases that were later undone lower it), (b) giving the human
specialist 'similar past cases' in the handoff brief, and (c) learning from human corrections.
"""
from __future__ import annotations

import threading
from typing import Any


class EpisodicMemory:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.items: list[dict[str, Any]] = []
        self._seed()

    def _seed(self) -> None:
        # Synthetic prior experience (clearly labelled in the UI as 'seeded'); real resolved cases get appended.
        seeds = [
            ("payment_failed", "REVERSAL_STUCK_TAT_BREACHED", "Alpha Bank", "1k-5k", "refund", True),
            ("payment_failed", "REVERSAL_STUCK_TAT_BREACHED", "Alpha Bank", "<1k", "refund", True),
            ("payment_failed", "REVERSAL_PENDING_WITHIN_TAT", "Beta Bank", "<1k", "inform", True),
            ("duplicate_debit", "DUPLICATE_DEBIT_CONFIRMED", "Gamma Bank", "1k-5k", "refund", True),
            ("settlement_held", "BANK_NAME_MISMATCH", "-", "10k-50k", "reverify+release", True),
            ("settlement_held", "BANK_NAME_MISMATCH", "-", "10k-50k", "reverify+release", True),
            ("settlement_held", "RISK_FRAUD_REVIEW", "-", ">50k", "escalate", True),
            ("dispute", "CHARGEBACK_EVIDENCE_COMPLETE", "-", "5k-10k", "representment", True),
            ("emi_query", "EMI_DUE_RESCHEDULE_ELIGIBLE", "-", "1k-5k", "reschedule", True),
            ("soundbox", "DEVICE_OFFLINE_RECOVERABLE", "-", "-", "reboot", True),
        ]
        for intent, rc, bank, band, action, ok in seeds:
            self.items.append({"id": f"seed-{len(self.items)}", "features": {f"intent:{intent}", f"rc:{rc}", f"bank:{bank}", f"band:{band}"},
                               "intent": intent, "root_cause": rc, "action": action, "success": ok, "seeded": True})

    @staticmethod
    def band(amount: float) -> str:
        return "-" if not amount else "<1k" if amount < 1000 else "1k-5k" if amount < 5000 else "5k-10k" if amount < 10000 else "10k-50k" if amount < 50000 else ">50k"

    def features(self, intent: str, rc: str, bank: str = "-", amount: float = 0.0) -> set[str]:
        return {f"intent:{intent}", f"rc:{rc}", f"bank:{bank}", f"band:{self.band(amount)}"}

    def similar(self, feats: set[str], k: int = 3) -> list[dict[str, Any]]:
        scored = []
        with self._lock:
            for it in self.items:
                inter = len(feats & it["features"])
                union = len(feats | it["features"]) or 1
                scored.append((inter / union, it))
        scored.sort(key=lambda x: -x[0])
        return [{"id": it["id"], "similarity": round(s, 2), "intent": it["intent"], "root_cause": it["root_cause"], "action": it["action"],
                 "success": it["success"], "seeded": it.get("seeded", False)} for s, it in scored[:k] if s >= 0.5]

    def calibrate(self, base: float, feats: set[str]) -> tuple[float, str]:
        sims = self.similar(feats, 5)
        if not sims:
            return base, "no similar past cases: confidence unchanged"
        ok = sum(1 for s in sims if s["success"])
        bad = len(sims) - ok
        if bad:
            return round(max(0.0, base - 0.08 * bad), 3), f"{bad} of {len(sims)} similar past cases failed or were undone: confidence lowered"
        if len(sims) >= 3:
            return round(min(0.99, base + 0.02), 3), f"{ok} similar past cases all succeeded: confidence +0.02"
        return base, f"{ok} similar past case(s) succeeded: confidence unchanged"

    def add(self, case_id: str, feats: set[str], intent: str, rc: str, action: str, success: bool) -> None:
        with self._lock:
            self.items.append({"id": case_id, "features": feats, "intent": intent, "root_cause": rc, "action": action, "success": success})

    def mark_failed(self, case_id: str) -> None:
        with self._lock:
            for it in self.items:
                if it["id"] == case_id:
                    it["success"] = False
