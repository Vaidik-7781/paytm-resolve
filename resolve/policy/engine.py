"""Policy-as-code engine. Deterministic, auditable, and separate from every model.

Monotone rule: every rule can only RAISE the required oversight tier, never lower it, so the safest rule wins.
  Tier 0  auto-resolve and notify
  Tier 1  act, notify, one-tap undo
  Tier 2  human approves the proposed actions (agent prepares everything)
  Tier 3  human only (agent prepares the handoff brief)
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from ..models import PolicyDecision, ProposedAction

POLICY_PATH = Path(__file__).with_name("policy.yaml")
LABELS = {0: "AUTO_RESOLVE", 1: "ACT_AND_NOTIFY", 2: "HUMAN_APPROVAL", 3: "HUMAN_ONLY"}


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@dataclass
class PolicyInput:
    category: str
    confidence: float
    actions: list[ProposedAction]
    flags: list[str] = field(default_factory=list)  # injection_attempt, scam_suspected, legal_threat
    risk_flags: list[str] = field(default_factory=list)  # from systems of record (fraud_review ...)
    authz_ok: bool = True
    consent: bool = False
    human_approved: bool = False
    duplicate_action: bool = False
    critic: str = "approve"  # approve | revise | reject
    gateway_failed: bool = False


class AutonomyBook:
    """Per-category autonomy level + evidence. Progressive autonomy: a category earns Tier 1 by precision."""

    def __init__(self, policy: dict[str, Any]) -> None:
        self._lock = threading.Lock()
        self.modes: dict[str, str] = {k: v["mode"] for k, v in policy["categories"].items()}
        # Seeded from a synthetic shadow-mode replay (clearly labelled in the UI). Real history replaces this.
        self.stats: dict[str, dict[str, int]] = {
            "payment_failed": {"decisions": 120, "correct": 118, "undone": 1, "rejected": 1},
            "duplicate_debit": {"decisions": 64, "correct": 63, "undone": 0, "rejected": 1},
            "settlement_held": {"decisions": 90, "correct": 87, "undone": 1, "rejected": 2},
            "dispute": {"decisions": 41, "correct": 38, "undone": 0, "rejected": 3},
            "emi_query": {"decisions": 52, "correct": 51, "undone": 0, "rejected": 1},
            "claim_status": {"decisions": 70, "correct": 69, "undone": 0, "rejected": 1},
            "soundbox": {"decisions": 80, "correct": 79, "undone": 0, "rejected": 1},
            "unknown": {"decisions": 25, "correct": 15, "undone": 0, "rejected": 10},
        }
        self.live: dict[str, dict[str, int]] = {}

    def mode(self, category: str) -> str:
        return self.modes.get(category, "shadow")

    def set_mode(self, category: str, mode: str) -> None:
        if mode not in ("autonomous", "assisted", "shadow"):
            raise ValueError("mode must be autonomous|assisted|shadow")
        with self._lock:
            self.modes[category] = mode

    def record(self, category: str, outcome: str) -> None:
        with self._lock:
            s = self.live.setdefault(category, {"decisions": 0, "correct": 0, "undone": 0, "rejected": 0})
            s["decisions"] += 1
            if outcome in ("correct", "undone", "rejected"):
                s[outcome] += 1

    def precision(self, category: str) -> float:
        base = self.stats.get(category, {"decisions": 0, "correct": 0})
        live = self.live.get(category, {"decisions": 0, "correct": 0})
        d = base["decisions"] + live["decisions"]
        c = base["correct"] + live["correct"]
        return round(c / d, 4) if d else 0.0

    def summary(self) -> list[dict[str, Any]]:
        out = []
        for cat in self.modes:
            base = self.stats.get(cat, {"decisions": 0, "correct": 0, "undone": 0, "rejected": 0})
            live = self.live.get(cat, {"decisions": 0, "correct": 0, "undone": 0, "rejected": 0})
            d = base["decisions"] + live["decisions"]
            p = self.precision(cat)
            out.append({
                "category": cat, "mode": self.modes[cat], "decisions": d, "precision": p,
                "undone": base["undone"] + live["undone"], "rejected": base["rejected"] + live["rejected"],
                "eligible_for_autonomy": d >= 40 and p >= 0.97,
                "live_decisions": live["decisions"],
            })
        return out


class PolicyEngine:
    def __init__(self, policy: Optional[dict[str, Any]] = None, book: Optional[AutonomyBook] = None) -> None:
        self.policy = policy or load_policy()
        self.book = book or AutonomyBook(self.policy)

    @property
    def version(self) -> str:
        return self.policy["version"]

    def action_class(self, tool: str) -> str:
        for cls, tools in self.policy["action_classes"].items():
            if tool in tools:
                return cls
        return "state_change"

    def evaluate(self, pi: PolicyInput) -> PolicyDecision:
        lim = self.policy["limits"]
        conf = self.policy["confidence"]
        reasons: list[dict[str, str]] = []
        tier = 0
        constraints: dict[str, Any] = {}

        def raise_to(t: int, rule: str, detail: str) -> None:
            nonlocal tier
            reasons.append({"rule": rule, "effect": f"tier>={t}", "detail": detail})
            tier = max(tier, t)

        writes = [a for a in pi.actions if a.kind != "notify" and a.tool not in ("notify_party", "create_ticket")]
        escalate_only = bool(pi.actions) and all(a.kind == "escalate" for a in pi.actions)

        # ---- baseline from action class
        if not writes:
            reasons.append({"rule": "R00", "effect": "tier>=0", "detail": "No state-changing action proposed: information only."})
        else:
            classes = {self.action_class(a.tool) for a in writes}
            if classes <= {"low_risk"} and pi.confidence >= conf["tier0"]:
                reasons.append({"rule": "R00", "effect": "tier>=0", "detail": "Only low-risk, reversible actions at high confidence."})
            else:
                raise_to(1, "R00", "State-changing action: act, notify and keep one-tap undo.")

        # ---- hard blocks
        if not pi.authz_ok:
            raise_to(3, "R03", "Requested record does not belong to the requester (authorization failed).")
        if pi.risk_flags or "scam_suspected" in pi.flags:
            raise_to(3, "R02", f"Risk/fraud signal present: {', '.join(pi.risk_flags or ['scam_suspected'])}.")
        if "legal_threat" in pi.flags:
            raise_to(3, "R12", "Legal or regulator language detected: human handles.")
        if pi.duplicate_action:
            raise_to(3, "R10", "Same action already executed for this entity (duplicate-action guard).")
        if pi.critic == "reject":
            raise_to(3, "R11", "Critic agent rejected the plan.")
        elif pi.critic == "revise":
            raise_to(2, "R11", "Critic agent asked for revision: human review.")
        if pi.gateway_failed:
            raise_to(3, "R13", "Gateway/system failure while executing: human handoff.")

        # ---- injection: never let free text drive money
        if "injection_attempt" in pi.flags:
            constraints["security_ticket"] = True
            if writes:
                raise_to(2, "R01", "Prompt-injection attempt detected: any state change needs human approval.")
            else:
                reasons.append({"rule": "R01", "effect": "monitor", "detail": "Prompt-injection attempt detected; no state change proposed, so the genuine issue is answered and a security ticket is opened."})

        # ---- autonomy level for this workflow
        mode = self.book.mode(pi.category)
        if writes and mode == "assisted":
            raise_to(2, "R05", f"Category '{pi.category}' is in assisted mode: writes need human approval.")
        elif mode == "shadow":
            raise_to(2, "R05", f"Category '{pi.category}' is in shadow mode: suggestions only.")

        # ---- confidence
        if pi.confidence < conf["tier2"]:
            raise_to(3, "R06", f"Confidence {pi.confidence:.2f} below {conf['tier2']}: not safe to act or approve blindly.")
        elif pi.confidence < conf["tier1"] and writes:
            raise_to(2, "R06", f"Confidence {pi.confidence:.2f} below {conf['tier1']}: human approval.")
        elif pi.confidence < conf["tier0"] and tier == 0 and writes:
            raise_to(1, "R06", f"Confidence {pi.confidence:.2f} below {conf['tier0']}: act with undo.")

        # ---- value limits per tool
        for a in writes:
            if a.value > lim["hard_cap_per_action"]:
                raise_to(3, "R04", f"{a.tool} value {a.value:,.0f} above hard cap {lim['hard_cap_per_action']:,}.")
            elif a.value > lim["tier2_max_value"]:
                raise_to(3, "R07", f"{a.tool} value {a.value:,.0f} above human-approvable limit {lim['tier2_max_value']:,}.")
            else:
                ceiling = lim["tier1_max_value"].get(a.tool, 0)
                if a.value > ceiling:
                    raise_to(2, "R07", f"{a.tool} value {a.value:,.0f} above auto limit {ceiling:,}.")
            if not a.reversible:
                raise_to(2, "R08", f"{a.tool} is not reversible: human approval.")

        # ---- consent for sales-type actions
        if any(a.kind == "sales" for a in writes) and not pi.consent:
            constraints["needs_consent"] = True
            reasons.append({"rule": "R09", "effect": "needs_consent", "detail": "Sales action requires explicit customer consent first."})

        # ---- escalation-only plans are always human
        if escalate_only:
            tier = max(tier, 3)

        auto = tier in (0, 1) and not constraints.get("needs_consent")
        requires_human = tier >= 2
        label = LABELS[tier]
        if tier == 2 and pi.human_approved:
            auto = True
            label = "APPROVED_BY_HUMAN"
            reasons.append({"rule": "R14", "effect": "approved", "detail": "A human approved the proposed actions in the console."})
        return PolicyDecision(tier=tier, label=label, auto=auto, requires_human=requires_human,
                              reasons=reasons, version=self.version, constraints=constraints)
