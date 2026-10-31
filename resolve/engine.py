"""Paytm Resolve orchestrator.

Case flow (every step is a streamed, auditable trace event):
  intake (guard, language, quarantined intent) -> supervisor plan (parallel specialists + retrieval + memory)
  -> diagnosis (rules / evidence / calibration / constrained LLM for novel) -> playbook actions + DRY RUN
  -> critic -> POLICY GATE (tier) -> {Tier 0/1: execute via gateway with gate tokens, verify, notify, close
                                      Tier 2: prepare + wait for human approval
                                      Tier 3: human-only handoff brief}
Nothing here can touch a system of record except through the Gateway, and the Gateway refuses writes without a
gate token that only this orchestrator mints after the policy engine says yes.
"""
from __future__ import annotations

import asyncio
import statistics
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Optional

from .agents import reasoning, specialists
from .config import settings
from .gateway.gateway import CaseBudget, Gateway, ToolResult
from .guard import GuardResult, inspect, mask_pii
from .knowledge.kb import HybridRetriever
from .llm.i18n import detect_language, inr, parse_yes_no, render
from .llm.router import get_llm
from .memory.episodic import EpisodicMemory
from .mockworld.world import SETUPS, World, build_world
from .models import (ActionRecord, Case, CriticVerdict, Party, PolicyDecision, ProposedAction, RootCause, TraceEvent, utcnow)
from .events.stream import Hub, IncidentDetector, simulate_outage_txns
from .policy.engine import PolicyEngine, PolicyInput

# Modeled human baseline minutes per category (ILLUSTRATIVE; replace with Paytm ops data). Used only for the savings view.
HUMAN_BASELINE_MIN = {"payment_failed": 240, "duplicate_debit": 300, "settlement_held": 1440, "dispute": 720, "emi_query": 90, "claim_status": 120,
                      "soundbox": 360, "unknown": 480}
HUMAN_COST_INR, AGENT_COST_INR = 40.0, 6.0

CONSENT_RC = {"EMI_DUE_RESCHEDULE_ELIGIBLE"}
WAITING_RC = {"KYC_EXPIRED", "CLAIM_DOCS_PENDING", "CHARGEBACK_EVIDENCE_INCOMPLETE", "CLAIM_DOCS_ALREADY_REQUESTED"}
NOTIFY_TOOLS = {"notify_party", "create_ticket"}


class CaseCtx:
    def __init__(self, engine: "Engine", case: Case) -> None:
        self.engine = engine
        self.case = case
        self.guard: GuardResult = GuardResult()
        self.facts: dict[str, Any] = {}
        self.budget = CaseBudget()
        self.rc: Optional[RootCause] = None
        self.findings: list[specialists.Findings] = []
        self.acts: list[ProposedAction] = []
        self.entities: dict[str, Any] = {}

    async def emit(self, agent: str, kind: str, title: str, detail: str = "", data: Optional[dict[str, Any]] = None) -> TraceEvent:
        ev = TraceEvent(case_id=self.case.id, agent=agent, kind=kind, title=title, detail=detail, data=data or {})
        self.case.trace.append(ev)
        self.engine.hub.publish("trace", ev.model_dump(mode="json"))
        if self.engine.pace:
            await asyncio.sleep(self.engine.pace)
        return ev

    async def read(self, agent: str, tool: str, args: dict[str, Any], owner_of=None) -> ToolResult:
        res = self.engine.gateway.call(self.case.id, tool, args, budget=self.budget, actor=self.case.party_id)
        self.case.tool_calls += 1
        brief = ", ".join(f"{k}={v}" for k, v in args.items())
        if res.error_code == "ACCESS_DENIED":
            await self.emit(agent, "tool_read", f"{tool}({brief})", "Gateway authorization: record belongs to another party. Details withheld, access refused and logged.",
                            {"tool": tool, "args": args, "ok": False, "redacted": True, "enforced_by": "gateway"})
            return res
        if res.ok:
            d = res.data
            short = ", ".join(f"{k}={v}" for k, v in list(d.items())[:5] if not isinstance(v, (dict, list)))[:200] or f"{len(d.get('items', []))} item(s)"
        else:
            short = f"error {res.error_code}"
        await self.emit(agent, "tool_read", f"{tool}({brief})", short, {"tool": tool, "args": args, "ok": res.ok, "latency_ms": res.latency_ms,
                                                                      "result": res.data if res.ok else {"error": res.error_code}})
        return res


class Engine:
    def __init__(self, world: Optional[World] = None, pace: Optional[float] = None, llm: Any = None, policy: Optional[PolicyEngine] = None, hub: Optional[Hub] = None) -> None:
        self.world = world or build_world()
        self.pace = settings.pace if pace is None else pace
        self.kb = HybridRetriever()
        self.policy = policy or PolicyEngine()
        self.gateway = Gateway(self.world, self.kb, settings.gate_secret, self.policy.policy["limits"]["hard_cap_per_action"])
        self.memory = EpisodicMemory()
        self.llm = llm or get_llm(reasoning.KNOWN)
        self.hub = hub or Hub()
        self.detector = IncidentDetector(self.world)
        self.cases: dict[str, Case] = {}
        self.ctxs: dict[str, CaseCtx] = {}
        self.tasks: set[asyncio.Task] = set()
        self.deflected = 0
        self.incident_cases: list[str] = []

    # ------------------------------------------------------------------ public API
    async def submit(self, party_id: str, message: str, channel: str = "chat", scenario: Optional[str] = None,
                     intent_hint: Optional[str] = None, wait: bool = False) -> Case:
        party = self.world.get_party(party_id)
        if not party:
            raise KeyError(f"unknown party {party_id}")
        case = Case(party_id=party_id, party_name=party.name, party_kind=party.kind, channel=channel, message=message, scenario=scenario)
        case.messages.append({"role": "customer" if message else "system", "text": mask_pii(message)[0] if message else f"[proactive case: {intent_hint}]", "ts": utcnow().isoformat()})
        self.cases[case.id] = case
        ctx = CaseCtx(self, case)
        ctx.facts["intent_hint"] = intent_hint
        self.ctxs[case.id] = ctx
        self._updated(case, "case.created")
        if wait:
            await self._safe_run(ctx)
        else:
            t = asyncio.create_task(self._safe_run(ctx))
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)
        return case

    async def wait_idle(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    def _updated(self, case: Case, type_: str = "case.updated") -> None:
        self.hub.publish(type_, self.case_summary(case))

    def case_summary(self, c: Case) -> dict[str, Any]:
        return {"id": c.id, "party_id": c.party_id, "party_name": c.party_name, "party_kind": c.party_kind, "channel": c.channel, "status": c.status,
                "intent": c.intent, "category": c.category, "tier": c.tier, "root_cause": c.root_cause.code if c.root_cause else None,
                "confidence": c.root_cause.confidence if c.root_cause else None, "flags": c.flags, "scenario": c.scenario,
                "created_at": c.created_at.isoformat(), "duration_s": c.duration_s, "summary": c.summary, "language": c.language,
                "tool_calls": c.tool_calls, "llm_calls": c.llm_calls, "message": c.message[:140]}

    async def _safe_run(self, ctx: CaseCtx) -> None:
        try:
            await self._run(ctx)
        except Exception as e:  # noqa: BLE001  never leave a case hanging
            traceback.print_exc()
            c = ctx.case
            await ctx.emit("Orchestrator", "error", "Unexpected error: escalating to a human", f"{type(e).__name__}: {e}")
            c.status = "ESCALATED"
            c.tier = 3
            c.summary = "Escalated after an internal error."
            c.duration_s = (utcnow() - c.created_at).total_seconds()
            self._updated(c)

    # ------------------------------------------------------------------ main pipeline
    async def _run(self, ctx: CaseCtx) -> None:
        case = ctx.case
        case.status = "WORKING"
        self._updated(case)
        party = self.world.get_party(case.party_id)
        assert party is not None
        proactive = case.channel in ("proactive", "event")

        # ---------------- 1. intake: guard, language, quarantined intent
        guard = inspect(case.message) if case.message else GuardResult()
        ctx.guard = guard
        case.flags = list(guard.flags)
        lang = detect_language(case.message) if case.message else party.language
        if lang == "en" and party.language in ("hi", "hinglish") and not case.message:
            lang = party.language
        case.language = lang
        if case.message:
            await ctx.emit("Intake", "intake", "Message received", f"language={lang}; PII tokenised {guard.pii_found or 'none'}; model sees only masked, injection-stripped text",
                           {"language": lang, "pii": guard.pii_found, "masked_preview": guard.masked_text[:200]})
        if guard.injection:
            await ctx.emit("Guard", "guard", "Prompt-injection attempt stripped", "Instruction-like text is treated as data and removed before any model sees it.",
                           {"removed": guard.removed, "targets_blocked": guard.injected_targets})
        if guard.scam:
            await ctx.emit("Guard", "guard", "Scam / social-engineering pattern detected", "Account actions from this conversation are disabled.", {"flags": guard.flags})
        if guard.legal:
            await ctx.emit("Guard", "guard", "Legal / regulator language detected", "Human specialist required.", {})
        if not proactive:
            ack = render("ack", lang, name=party.name.split()[0])
            case.messages.append({"role": "agent", "text": ack, "ts": utcnow().isoformat(), "kind": "ack"})

        hint = ctx.facts.get("intent_hint")
        if hint:
            intent, ents, conf = hint, {}, 0.99
            await ctx.emit("Intake", "intake", f"Proactive trigger: {hint}", "Opened by the event watcher before the user contacted support.", {"intent": hint})
        else:
            text_for_model = guard.masked_text or guard.clean_text or case.message
            if lang not in ("en", "hi", "hinglish"):
                from .llm.sarvam import sarvam
                if sarvam.enabled:
                    try:
                        eng_text = await sarvam.translate(text_for_model, "en", source=lang, purpose="understand")
                        g2 = inspect(eng_text)
                        guard.injection, guard.scam, guard.legal = guard.injection or g2.injection, guard.scam or g2.scam, guard.legal or g2.legal
                        case.flags = list(guard.flags)
                        await ctx.emit("Intake", "translate", f"Sarvam Mayura: {lang} → en", eng_text[:200], {"from": lang, "model": "mayura:v1"})
                        text_for_model = g2.masked_text or eng_text
                    except Exception as e:  # noqa: BLE001
                        await ctx.emit("Intake", "translate", "Translation unavailable", f"{type(e).__name__}: routing to a human", {})
                else:
                    await ctx.emit("Intake", "translate", f"Language '{lang}' needs Sarvam translation (no key configured)", "Routing to a human rather than guessing.", {})
            ctx.facts['text_for_model'] = text_for_model
            cls = await self.llm.classify_intent(text_for_model, lang)
            case.llm_calls += 1
            intent, ents, conf = cls["intent"], cls.get("entities", {}), cls["confidence"]
            votes = cls.get("votes")
            how = (f"votes {votes} → {cls.get('source')}; " if votes else "")
            await ctx.emit("Intake", "intent", f"Intent: {intent}", f"quarantined parser ({self.llm.name}); {how}confidence {conf:.2f}; entities {ents or 'none'}",
                           {"intent": intent, "confidence": conf, "entities": ents, "votes": votes, "source": cls.get("source"), "ml": cls.get("ml"), "rules": cls.get("rules")})
            ctx.facts["intent_conf"] = conf
        case.intent = intent
        ctx.entities = ents

        # ---------------- 2. supervisor plan + parallel context gathering
        if guard.scam:
            await self._scam_path(ctx)
            return
        plan = self._plan_nodes(intent)
        await ctx.emit("Supervisor", "plan", "Task graph", " | ".join(f"{n['agent']}: {n['goal']}" for n in plan), {"nodes": plan})

        async def retrieval() -> None:
            hits = self.kb.search(f"{intent.replace('_', ' ')} {ctx.guard.clean_text[:120]}", 3)
            ctx.facts["kb_hits"] = hits
            await ctx.emit("Knowledge agent", "retrieval", "Hybrid retrieval (BM25 + trigram, RRF)", "; ".join(f"{h['id']} {h['title']}" for h in hits), {"hits": hits})

        async def recall() -> None:
            sims = self.memory.similar({f"intent:{intent}"}, 3)
            ctx.facts["similar"] = sims
            await ctx.emit("Memory agent", "memory", "Episodic recall", f"{len(sims)} similar past case(s)" if sims else "no similar past cases", {"similar": sims})

        spec_tasks = self._specialist_tasks(ctx, intent, ents)
        results = await asyncio.gather(retrieval(), recall(), *spec_tasks)
        ctx.findings = [r for r in results if isinstance(r, specialists.Findings)]
        for f in ctx.findings:
            await ctx.emit(f.agent, "finding", "Finding", f.summary, {"signals": f.signals})

        # ---------------- 3. diagnosis
        rc = await reasoning.diagnose(ctx, intent, ctx.findings)
        ctx.rc = rc
        case.root_cause, case.category = rc, rc.category
        await ctx.emit("Diagnoser", "diagnosis", f"Root cause: {rc.code}", rc.rationale, {"code": rc.code, "confidence": rc.confidence, "citations": rc.citations,
                                                                                         "calibration": ctx.facts.get("calibration_note"), "source": rc.signals.get("source")})
        self._updated(case)

        # ---------------- 4. playbook actions + dry run
        acts = reasoning.plan_actions(ctx, rc, ctx.findings)
        for a in acts:
            if a.kind not in ("escalate", "notify") and a.tool not in NOTIFY_TOOLS:
                dr = self.gateway.call(case.id, a.tool, a.args, dry_run=True, budget=ctx.budget, actor=case.party_id)
                case.tool_calls += 1
                a.preview = dr.preview if dr.ok else {"error": dr.error_code}
                await ctx.emit("Planner", "dry_run", f"Dry run: {a.label}", "; ".join(f"{k}: {v[0]} -> {v[1]}" if isinstance(v, list) and len(v) == 2 else f"{k}: {v}" for k, v in a.preview.items())[:300],
                               {"tool": a.tool, "args": a.args, "preview": a.preview, "value": a.value})
        ctx.acts, case.proposed = acts, acts
        if not acts:
            await ctx.emit("Planner", "plan", "No state change needed", "Information-only resolution.", {})

        # ---------------- 5. critic
        verdict = await reasoning.critique(ctx, rc, acts, ctx.findings)
        case.critic = verdict
        await ctx.emit("Critic", "critic", f"Verdict: {verdict.verdict}", "; ".join(c["name"] + (" OK" if c["ok"] else " FAIL") for c in verdict.checks),
                       {"verdict": verdict.verdict, "checks": verdict.checks, "issues": verdict.issues})
        if verdict.confidence_delta:
            rc.confidence = round(max(0.0, rc.confidence + verdict.confidence_delta), 3)

        # ---------------- 6. policy gate
        decision = self._decide(ctx, verdict)
        case.policy, case.tier = decision, decision.tier
        await ctx.emit("Policy gate", "policy", f"Tier {decision.tier}: {decision.label}", " | ".join(f"{r['rule']}: {r['detail']}" for r in decision.reasons)[:400],
                       {"tier": decision.tier, "label": decision.label, "reasons": decision.reasons, "constraints": decision.constraints, "version": decision.version})
        self._updated(case)

        await self._route(ctx, decision)

    def _plan_nodes(self, intent: str) -> list[dict[str, Any]]:
        spec = {"payment_failed": ("Transaction tracer", "trace every leg of the payment"), "duplicate_debit": ("Transaction tracer", "find the duplicate debit"),
                "settlement_held": ("Settlement analyst", "find the hold reason and KYC match"), "dispute": ("Dispute builder", "assemble the evidence pack"),
                "emi_query": ("Lending agent", "read EMI, eligibility and hardship signal"), "claim_status": ("Claims agent", "read claim and missing documents"),
                "soundbox": ("Device agent", "read device health"), "unknown": ("Transaction tracer", "gather recent activity for triage")}[intent]
        return [{"id": "n1", "agent": "Knowledge agent", "goal": "retrieve SOPs and rules", "deps": []},
                {"id": "n2", "agent": "Memory agent", "goal": "recall similar past cases", "deps": []},
                {"id": "n3", "agent": spec[0], "goal": spec[1], "deps": []},
                {"id": "n4", "agent": "Diagnoser", "goal": "root cause with calibrated confidence", "deps": ["n1", "n2", "n3"]},
                {"id": "n5", "agent": "Critic", "goal": "re-check plan against evidence", "deps": ["n4"]},
                {"id": "n6", "agent": "Policy gate", "goal": "assign autonomy tier", "deps": ["n5"]}]

    def _specialist_tasks(self, ctx: CaseCtx, intent: str, ents: dict[str, Any]) -> list:
        S = specialists
        factory = {"payment_failed": lambda: S.transaction_tracer(ctx, ents), "duplicate_debit": lambda: S.transaction_tracer(ctx, ents, want_duplicate=True),
                   "settlement_held": lambda: S.settlement_analyst(ctx, ents), "dispute": lambda: S.dispute_builder(ctx, ents),
                   "emi_query": lambda: S.lending_agent(ctx, ents), "claim_status": lambda: S.claims_agent(ctx, ents),
                   "soundbox": lambda: S.device_agent(ctx, ents), "unknown": lambda: S.transaction_tracer(ctx, ents)}
        return [factory[intent]()]

    def _decide(self, ctx: CaseCtx, verdict: CriticVerdict, human_approved: bool = False) -> PolicyDecision:
        case, rc = ctx.case, ctx.rc
        assert rc is not None
        risk = []
        for f in ctx.findings:
            risk += list(f.signals.get("risk_flags") or [])
        dup = any(self._already_applied(a) for a in ctx.acts)
        pi = PolicyInput(category=rc.category, confidence=rc.confidence, actions=ctx.acts, flags=list(case.flags), risk_flags=risk,
                         authz_ok=not any(f.access_denied for f in ctx.findings) and rc.code != "ACCESS_DENIED_OTHER_ACCOUNT",
                         consent=bool(ctx.facts.get("consent")), human_approved=human_approved, duplicate_action=dup, critic=verdict.verdict)
        return self.policy.evaluate(pi)

    def _already_applied(self, a: ProposedAction) -> bool:
        """State-based duplicate-action guard: has this exact effect already been applied in the system of record?"""
        w, g = self.world, a.args
        if a.tool == "initiate_refund":
            t = w.get_txn(g["txn_id"]); return bool(t and t.refund_id)
        if a.tool == "release_settlement_hold":
            x = w.get_settlement(g["settlement_id"]); return bool(x and x.status == "SETTLED")
        if a.tool == "file_representment":
            d = w.get_dispute(g["dispute_id"]); return bool(d and d.status != "OPEN")
        if a.tool == "reschedule_emi":
            l = w.get_loan(g["loan_id"]); return bool(l and l.reschedules_last_12m >= 1)
        return False

    # ------------------------------------------------------------------ routing by tier
    async def _route(self, ctx: CaseCtx, decision: PolicyDecision) -> None:
        if decision.tier >= 3:
            await self._escalate(ctx, decision)
        elif decision.constraints.get("needs_consent"):
            await self._ask_consent(ctx, decision)
        elif decision.tier == 2 and not decision.auto:
            await self._request_approval(ctx, decision)
        else:
            await self._execute_and_close(ctx, decision)

    # ------------------------------------------------------------------ message helpers
    def _facts_for(self, ctx: CaseCtx, executed: Optional[dict[str, ToolResult]] = None) -> dict[str, Any]:
        rc, s = ctx.rc, ctx.rc.signals if ctx.rc else {}
        ex = executed or {}
        out: dict[str, Any] = {
            "amount": inr(s["amount"]) if s.get("amount") is not None else "", "txn": s.get("txn_id", ""), "eta_h": s.get("eta_hours", ""), "bank": s.get("bank", ""),
            "sid": s.get("settlement_id", ""), "did": s.get("dispute_id") or s.get("device_id") or "", "deadline": (s.get("deadline") or "")[:10],
            "missing": ", ".join(s.get("missing", [])), "emi": inr(s["emi"]) if s.get("emi") is not None else "", "due": s.get("due_date", ""),
            "new_due": s.get("new_due_date", ""), "outstanding": inr(s["outstanding"]) if s.get("outstanding") is not None else "",
            "cid": s.get("claim_id", ""), "docs": ", ".join(s.get("still_to_request") or s.get("missing_docs") or []), "status": (s.get("status") or "").replace("_", " ").lower(),
            "eta": s.get("eta_days", ""), "ticket": ctx.facts.get("ticket_id", "pending"),
        }
        if "initiate_refund" in ex and ex["initiate_refund"].ok:
            out["refund_id"] = ex["initiate_refund"].data["refund_id"]
        else:
            out["refund_id"] = s.get("refund_id") or ""
        return out

    def _message_kind(self, ctx: CaseCtx, executed: dict[str, ToolResult], escalated: bool = False) -> str:
        code = ctx.rc.code if ctx.rc else "UNKNOWN"
        if ctx.case.channel == "proactive" and code == "BANK_NAME_MISMATCH" and "release_settlement_hold" in executed:
            return "proactive_settlement"
        if code == "SCAM_SUSPECTED":
            return "scam_warning"
        if code == "ACCESS_DENIED_OTHER_ACCOUNT":
            return "access_denied"
        if escalated:
            if code == "CHARGEBACK_EVIDENCE_INCOMPLETE":
                return "dispute_need_evidence"
            return "novel_escalation" if code == "UNKNOWN" else "escalated"
        m = {"REVERSAL_PENDING_WITHIN_TAT": "payment_reversal_pending", "BANK_INCIDENT_AUTO_REVERSAL": "proactive_outage",
             "REVERSAL_STUCK_TAT_BREACHED": "payment_refund_initiated", "ALREADY_REFUNDED": "payment_refund_initiated",
             "DUPLICATE_DEBIT_CONFIRMED": "duplicate_refund", "PAYMENT_ACTUALLY_SUCCESSFUL": "payment_success_info", "NO_DEBIT_OCCURRED": "no_debit_info",
             "BANK_NAME_MISMATCH": "settlement_released", "KYC_EXPIRED": "settlement_need_docs", "SETTLEMENT_PROCESSING_NORMAL": "settlement_pending_normal",
             "CHARGEBACK_EVIDENCE_COMPLETE": "dispute_filed", "CHARGEBACK_EVIDENCE_INCOMPLETE": "dispute_need_evidence",
             "EMI_DUE_RESCHEDULE_ELIGIBLE": "emi_rescheduled", "EMI_INFO_ONLY": "emi_info", "CLAIM_DOCS_PENDING": "claim_docs_needed",
             "CLAIM_DOCS_ALREADY_REQUESTED": "claim_docs_needed", "CLAIM_IN_REVIEW": "claim_status", "DEVICE_OFFLINE_RECOVERABLE": "soundbox_rebooted",
             "DEVICE_ACTIVE": "soundbox_rebooted", "DEVICE_OUT_OF_WARRANTY": "escalated"}
        kind = m.get(code, "escalated")
        if code == "DEVICE_OFFLINE_RECOVERABLE" and "dispatch_replacement" in executed and executed["dispatch_replacement"].ok:
            kind = "soundbox_replacement"
        return kind

    def _token(self, ctx: CaseCtx, tool: str, args: dict[str, Any], tier: int) -> str:
        return self.gateway.issue_token(ctx.case.id, tool, args, 0 if tool in NOTIFY_TOOLS else tier)

    async def _send_message(self, ctx: CaseCtx, kind: str, executed: Optional[dict[str, ToolResult]] = None, extra_note: bool = True) -> str:
        case = ctx.case
        facts = self._facts_for(ctx, executed)
        text = await self.llm.compose(kind, case.language, facts)
        case.llm_calls += 1 if self.llm.name != "offline-deterministic" else 0
        if extra_note and "injection_attempt" in case.flags:
            text += " " + render("injection_note", case.language)
        if case.language not in ("en", "hi", "hinglish"):
            text = await self._translate(text, case.language)
        args = {"party_id": case.party_id, "text": text, "kind": kind, "lang": case.language, "case_id": case.id}
        res = self.gateway.call(case.id, "notify_party", args, token=self._token(ctx, "notify_party", args, 0), budget=ctx.budget)
        case.tool_calls += 1
        case.messages.append({"role": "agent", "text": text, "ts": utcnow().isoformat(), "kind": kind, "lang": case.language})
        await ctx.emit("Comms agent", "comms", f"Message sent ({case.language})", text, {"kind": kind, "lang": case.language, "notification": res.data})
        self.hub.publish("message", {"case_id": case.id, "text": text, "lang": case.language, "kind": kind})
        return text

    async def _translate(self, text: str, lang: str) -> str:
        from .llm.sarvam import sarvam
        if sarvam.enabled:
            try:
                return await sarvam.translate(text, lang, source="en", purpose="reply")
            except Exception:  # noqa: BLE001
                return text
        return text

    # ------------------------------------------------------------------ tier 3
    async def _open_ticket(self, ctx: CaseCtx, act: ProposedAction) -> Optional[str]:
        res = self.gateway.call(ctx.case.id, "create_ticket", act.args, token=self._token(ctx, "create_ticket", act.args, 0), budget=ctx.budget)
        ctx.case.tool_calls += 1
        if res.ok:
            ctx.facts.setdefault("ticket_id", res.data["ticket_id"])
            await ctx.emit("Escalation agent", "tool_write", f"create_ticket → {res.data['ticket_id']} ({res.data['queue']})", act.args["summary"],
                           {"tool": "create_ticket", "args": act.args, "ok": True, "ledger_id": res.ledger_id})
            ctx.case.actions.append(ActionRecord(id=act.id, tool=act.tool, label=act.label, args=act.args, status="EXECUTED", result=res.data, reversible=False,
                                                 compensation_token=res.ledger_id, tier=3))
            return res.data["ticket_id"]
        return None

    def _brief(self, ctx: CaseCtx, decision: PolicyDecision, suggested_reply: str, reason: str) -> dict[str, Any]:
        rc = ctx.rc
        assert rc is not None
        recommended = [a.label for a in ctx.acts if a.tool != "create_ticket"] or ["Review manually"]
        return {
            "headline": f"{ctx.case.party_name}: {ctx.case.intent} | {rc.code}", "why_human": reason,
            "root_cause": {"code": rc.code, "confidence": rc.confidence, "rationale": rc.rationale, "citations": rc.citations},
            "evidence": [{"agent": f.agent, "summary": f.summary} for f in ctx.findings], "risk_flags": [*(ctx.case.flags)] + [x for f in ctx.findings for x in f.signals.get("risk_flags", [])],
            "recommended_actions": recommended, "policy": [f"{r['rule']}: {r['detail']}" for r in decision.reasons],
            "critic_issues": ctx.case.critic.issues if ctx.case.critic else [], "similar_cases": ctx.facts.get("similar", []),
            "suggested_reply": suggested_reply, "tier": decision.tier, "ai_investigation": ctx.facts.get("investigation"),
        }

    async def _escalate(self, ctx: CaseCtx, decision: PolicyDecision) -> None:
        case, rc = ctx.case, ctx.rc
        assert rc is not None
        for a in ctx.acts:
            if a.tool == "create_ticket":
                await self._open_ticket(ctx, a)
        kind = self._message_kind(ctx, {}, escalated=True)
        text = await self._send_message(ctx, kind)
        reason = "; ".join(r["detail"] for r in decision.reasons if r["effect"].startswith("tier>=3")) or "Policy requires a human."
        case.brief = self._brief(ctx, decision, text, reason)
        await ctx.emit("Escalation agent", "escalate", "Human-only handoff brief prepared", reason, {"brief": case.brief})
        case.status = "ESCALATED"
        self._finish(ctx, f"Escalated (Tier 3): {reason}")

    # ------------------------------------------------------------------ tier 2
    async def _request_approval(self, ctx: CaseCtx, decision: PolicyDecision) -> None:
        case = ctx.case
        for a in ctx.acts:
            if a.tool == "create_ticket":
                await self._open_ticket(ctx, a)
        holds = [a for a in ctx.acts if a.tool != "create_ticket"]
        case.approval = {"requested_at": utcnow().isoformat(), "actions": [a.model_dump(mode="json") for a in holds],
                         "reasons": [r for r in decision.reasons if r["effect"].startswith("tier>=2")]}
        pending_kind = "dispute_pending_approval" if ctx.rc and ctx.rc.category == "dispute" else "approval_wait"
        text = await self._send_message(ctx, pending_kind)
        case.brief = self._brief(ctx, decision, text, "; ".join(r["detail"] for r in case.approval["reasons"]) or "Human approval required.")
        await ctx.emit("Approval queue", "approval", "Waiting for human approval", "Everything is prepared: evidence, dry-run diffs, policy reasons. One tap to approve.",
                       {"actions": case.approval["actions"], "brief": case.brief})
        case.status = "AWAITING_APPROVAL"
        case.summary = "Prepared and waiting for human approval (Tier 2)."
        self._updated(case)

    async def approve(self, case_id: str, approver: str = "ops.lead", note: str = "") -> Case:
        case, ctx = self.cases[case_id], self.ctxs[case_id]
        if case.status != "AWAITING_APPROVAL" or not ctx.rc:
            raise ValueError("case is not awaiting approval")
        await ctx.emit("Human", "approval", f"Approved by {approver}", note or "Approved as proposed.", {"approver": approver})
        verdict = case.critic or CriticVerdict(verdict="approve", checks=[])
        decision = self._decide(ctx, verdict, human_approved=True)
        case.policy, case.tier = decision, 2
        await ctx.emit("Policy gate", "policy", "Re-evaluated with human approval", "; ".join(r["detail"] for r in decision.reasons[-2:]), {"tier": decision.tier, "label": decision.label})
        if decision.tier >= 3:
            await self._escalate(ctx, decision)
            return case
        self.policy.book.record(ctx.rc.category, "correct")
        await self._execute_and_close(ctx, decision, approved_by=approver)
        return case

    async def reject(self, case_id: str, approver: str = "ops.lead", note: str = "") -> Case:
        case, ctx = self.cases[case_id], self.ctxs[case_id]
        if case.status != "AWAITING_APPROVAL":
            raise ValueError("case is not awaiting approval")
        await ctx.emit("Human", "approval", f"Rejected by {approver}", note or "Rejected; handled manually.", {"approver": approver})
        if ctx.rc:
            self.policy.book.record(ctx.rc.category, "rejected")
        case.status = "ESCALATED"
        self._finish(ctx, f"Rejected by {approver}; handled manually.")
        return case

    # ------------------------------------------------------------------ consent (sales-type actions)
    async def _ask_consent(self, ctx: CaseCtx, decision: PolicyDecision) -> None:
        case = ctx.case
        text = await self._send_message(ctx, "emi_offer")
        case.pending_consent = {"actions": [a.id for a in ctx.acts if a.kind == "sales"]}
        await ctx.emit("Policy gate", "consent", "Explicit customer consent required before any sales-type action", "Offer sent; waiting for the customer's reply.", {"offer": text})
        case.status = "AWAITING_CUSTOMER"
        case.summary = "Offer sent; waiting for consent."
        self._updated(case)

    async def reply(self, case_id: str, text: str) -> Case:
        case, ctx = self.cases[case_id], self.ctxs[case_id]
        case.messages.append({"role": "customer", "text": mask_pii(text)[0], "ts": utcnow().isoformat()})
        self._updated(case)
        if not case.pending_consent:
            await ctx.emit("Intake", "intake", "Customer follow-up received", mask_pii(text)[0][:160], {})
            return case
        ans = parse_yes_no(text)
        await ctx.emit("Intake", "consent", f"Consent reply parsed: {ans}", mask_pii(text)[0][:120], {"answer": ans})
        if ans == "yes":
            ctx.facts["consent"] = True
            case.pending_consent = None
            case.status = "WORKING"
            verdict = case.critic or CriticVerdict(verdict="approve", checks=[])
            decision = self._decide(ctx, verdict)
            case.policy, case.tier = decision, decision.tier
            await ctx.emit("Policy gate", "policy", f"Tier {decision.tier}: {decision.label} (consent recorded)", "; ".join(r["detail"] for r in decision.reasons)[:300],
                           {"tier": decision.tier, "label": decision.label, "reasons": decision.reasons})
            await self._route(ctx, decision)
        elif ans == "no":
            case.pending_consent = None
            await self._send_message(ctx, "emi_declined")
            case.status = "RESOLVED"
            self._finish(ctx, "Customer declined the offer; nothing changed.")
        else:
            await self._send_message(ctx, "emi_offer")
        return case

    # ------------------------------------------------------------------ tier 0/1 execution
    @staticmethod
    def _precondition_ok(pre: Optional[str], results: dict[str, ToolResult]) -> bool:
        if not pre:
            return True
        neg = pre.startswith("!")
        key = pre.lstrip("!")
        tool, _, field = key.partition(".")
        r = results.get(tool)
        val = bool(r and r.ok and (r.data.get(field) if field else True))
        return (not val) if neg else val

    async def _execute_and_close(self, ctx: CaseCtx, decision: PolicyDecision, approved_by: Optional[str] = None) -> None:
        case = ctx.case
        results: dict[str, ToolResult] = {}
        done: list[ActionRecord] = []
        for a in ctx.acts:
            if a.tool == "create_ticket":
                await self._open_ticket(ctx, a)
                continue
            if not self._precondition_ok(a.precondition, results):
                await ctx.emit("Executor", "skip", f"Skipped: {a.label}", f"precondition '{a.precondition}' not met", {"tool": a.tool})
                case.actions.append(ActionRecord(id=a.id, tool=a.tool, label=a.label, args=a.args, status="SKIPPED", tier=decision.tier))
                continue
            token = self._token(ctx, a.tool, a.args, decision.tier)
            res: ToolResult = ToolResult(ok=False, tool=a.tool)
            for attempt in range(1, 4):
                res = self.gateway.call(case.id, a.tool, a.args, token=token, budget=ctx.budget, actor=case.party_id)
                case.tool_calls += 1
                await ctx.emit("Executor", "tool_write", f"{a.tool} (attempt {attempt}) → {'OK' if res.ok else res.error_code}",
                               a.label + (" [idempotent replay]" if res.replayed else ""),
                               {"tool": a.tool, "args": a.args, "ok": res.ok, "error": res.error_code, "latency_ms": res.latency_ms, "result": res.data,
                                "ledger_id": res.ledger_id, "tier": decision.tier, "token_issued_by": "policy-gate"})
                if res.ok or not res.retryable:
                    break
                await asyncio.sleep(self.pace * 0.5)
            if not res.ok:
                await self._fail_and_compensate(ctx, done, a, res)
                return
            results[a.tool] = res
            rec = ActionRecord(id=a.id, tool=a.tool, label=a.label, args=a.args, status="EXECUTED", result=res.data, value=a.value, reversible=a.reversible,
                               compensation_token=res.ledger_id, tier=decision.tier, approved_by=approved_by)
            case.actions.append(rec)
            done.append(rec)
        ok = await self._verify(ctx, done, results)
        if not ok:
            await self._fail_and_compensate(ctx, done, None, ToolResult(ok=False, tool="verify", error_code="VERIFICATION_FAILED"))
            return
        kind = self._message_kind(ctx, results)
        await self._send_message(ctx, kind, results)
        code = ctx.rc.code if ctx.rc else ""
        case.status = "AWAITING_CUSTOMER" if code in WAITING_RC else "RESOLVED"
        if ctx.rc and decision.tier <= 1 and not approved_by:
            self.policy.book.record(ctx.rc.category, "correct")
        if ctx.rc:
            self.memory.add(case.id, ctx.facts.get("features", set()), case.intent or "", code, "+".join(a.tool for a in done) or "inform", True)
        self._finish(ctx, f"{'Resolved' if case.status == 'RESOLVED' else 'Waiting on customer'} at Tier {decision.tier}" + (f", approved by {approved_by}" if approved_by else "") + ".")

    async def _verify(self, ctx: CaseCtx, done: list[ActionRecord], results: dict[str, ToolResult]) -> bool:
        ok_all = True
        for rec in done:
            a, t = rec.args, rec.tool
            good, detail = True, "no read-back needed"
            if t == "initiate_refund":
                r = await ctx.read("Verifier", "get_txn", {"txn_id": a["txn_id"]})
                good = bool(r.ok and r.data.get("refund_id")); detail = f"ledger shows refund {r.data.get('refund_id')}"
            elif t == "release_settlement_hold":
                r = await ctx.read("Verifier", "get_settlement", {"settlement_id": a["settlement_id"]})
                good = bool(r.ok and r.data.get("status") == "SETTLED"); detail = f"settlement status {r.data.get('status')}"
            elif t == "reschedule_emi":
                r = await ctx.read("Verifier", "get_loan", {"loan_id": a["loan_id"]})
                good = bool(r.ok and r.data["due_date"][:10] == a["new_due_date"][:10]); detail = f"loan due date {r.data.get('due_date', '')[:10]}"
            elif t == "request_documents":
                r = await ctx.read("Verifier", "get_claim", {"claim_id": a["claim_id"]})
                good = bool(r.ok and set(a["docs"]) <= set(r.data.get("requested_docs", []))); detail = f"requested docs {r.data.get('requested_docs')}"
            elif t in ("remote_reboot", "dispatch_replacement"):
                r = await ctx.read("Verifier", "get_device", {"device_id": a["device_id"]})
                st = r.data.get("status")
                good = st in ("ACTIVE", "REPLACEMENT_DISPATCHED") or (t == "remote_reboot" and "dispatch_replacement" in results)
                detail = f"device status {st}"
            elif t == "file_representment":
                r = await ctx.read("Verifier", "get_dispute", {"dispute_id": a["dispute_id"]})
                good = bool(r.ok and r.data.get("status") == "REPRESENTMENT_FILED"); detail = f"dispute status {r.data.get('status')}"
            elif t in ("trigger_reverification", "validate_bank_account"):
                r = await ctx.read("Verifier", "get_kyc", {"merchant_id": a["merchant_id"]})
                good = bool(r.ok and r.data.get("reverification") in ("IN_PROGRESS", "PASSED")); detail = f"re-verification {r.data.get('reverification')}"
            await ctx.emit("Verifier", "verify", f"{'Verified' if good else 'VERIFICATION FAILED'}: {t}", detail, {"tool": t, "ok": good})
            ok_all = ok_all and good
        return ok_all

    async def _fail_and_compensate(self, ctx: CaseCtx, done: list[ActionRecord], failed: Optional[ProposedAction], res: ToolResult) -> None:
        case = ctx.case
        await ctx.emit("Executor", "error", f"Action failed: {failed.tool if failed else 'verification'} → {res.error_code}",
                       "Rolling back completed steps (saga compensation) and handing to a human.", {"error": res.error_code})
        for rec in reversed(done):
            if rec.compensation_token and rec.reversible:
                u = self.gateway.undo(rec.compensation_token)
                case.tool_calls += 1
                rec.status = "UNDONE" if u.ok else rec.status
                await ctx.emit("Executor", "compensate", f"Compensated: {rec.tool}", "undo ok" if u.ok else f"undo failed {u.error_code}", {"ok": u.ok})
        if failed:
            case.actions.append(ActionRecord(id=failed.id, tool=failed.tool, label=failed.label, args=failed.args, status="FAILED", result={"error": res.error_code}, tier=case.tier or 0))
        tk = self.gateway.call(case.id, "create_ticket", {"case_id": case.id, "queue": "ops-triage", "priority": "P2",
                                                           "summary": f"Automation stopped at {failed.tool if failed else 'verification'}: {res.error_code}. Completed steps were rolled back."},
                               token=self._token(ctx, "create_ticket", {"case_id": case.id, "queue": "ops-triage", "priority": "P2",
                                                                          "summary": f"Automation stopped at {failed.tool if failed else 'verification'}: {res.error_code}. Completed steps were rolled back."}, 0))
        if tk.ok:
            ctx.facts["ticket_id"] = tk.data["ticket_id"]
            case.actions.append(ActionRecord(id=f"tk_{tk.data['ticket_id']}", tool="create_ticket", label="Escalate after failure", args={"case_id": case.id, "queue": "ops-triage"},
                                             status="EXECUTED", result=tk.data, reversible=False, compensation_token=tk.ledger_id, tier=3))
        dec = case.policy or PolicyDecision(tier=3, label="HUMAN_ONLY", auto=False, requires_human=True, reasons=[])
        dec = PolicyDecision(tier=3, label="HUMAN_ONLY", auto=False, requires_human=True,
                             reasons=[*dec.reasons, {"rule": "R13", "effect": "tier>=3", "detail": f"Execution failed ({res.error_code}); rolled back."}])
        case.policy, case.tier = dec, 3
        await self._send_message(ctx, "action_failed_escalation")
        case.brief = self._brief(ctx, dec, case.messages[-1]["text"], f"Automation failed: {res.error_code}")
        case.status = "ESCALATED"
        self._finish(ctx, f"Escalated after failure ({res.error_code}); completed steps rolled back.")

    # ------------------------------------------------------------------ scam path (no account actions from the conversation)
    async def _scam_path(self, ctx: CaseCtx) -> None:
        case = ctx.case
        case.category = "unknown"
        f = await specialists.transaction_tracer(ctx, {})
        ctx.findings = [f]
        rc = RootCause(code="SCAM_SUSPECTED", confidence=0.97, rationale="Customer describes sharing an OTP/screen with a caller: probable social-engineering scam.",
                       category="unknown", citations=["SEC-SCAM-01"], signals={"recent_activity": f.summary})
        ctx.rc, case.root_cause = rc, rc
        await ctx.emit("Diagnoser", "diagnosis", "Root cause: SCAM_SUSPECTED", rc.rationale, {"code": rc.code, "confidence": rc.confidence, "citations": rc.citations})
        ctx.acts = reasoning.plan_actions(ctx, rc, ctx.findings)
        case.proposed = ctx.acts
        verdict = await reasoning.critique(ctx, rc, ctx.acts, ctx.findings)
        case.critic = verdict
        decision = self._decide(ctx, verdict)
        case.policy, case.tier = decision, decision.tier
        await ctx.emit("Policy gate", "policy", f"Tier {decision.tier}: {decision.label}", " | ".join(f"{r['rule']}: {r['detail']}" for r in decision.reasons)[:300],
                       {"tier": decision.tier, "label": decision.label, "reasons": decision.reasons})
        await self._escalate(ctx, decision)

    # ------------------------------------------------------------------ completion + human actions
    def _finish(self, ctx: CaseCtx, summary: str) -> None:
        case = ctx.case
        case.summary = summary
        if case.status in ("RESOLVED", "ESCALATED", "CLOSED"):
            case.resolved_at = utcnow()
        case.duration_s = round((utcnow() - case.created_at).total_seconds(), 2)
        from .audit import seal
        seal(case)
        self._updated(case)
        self.hub.publish("metrics", self.metrics())

    async def human_resolve(self, case_id: str, resolver: str, note: str, outcome: str = "resolved") -> Case:
        case, ctx = self.cases[case_id], self.ctxs[case_id]
        await ctx.emit("Human", "human", f"Closed by {resolver}", note or "Resolved manually.", {"outcome": outcome})
        case.status = "CLOSED"
        if ctx.rc:
            self.memory.add(case.id, ctx.facts.get("features", set()), case.intent or "", ctx.rc.code, "human:" + (note[:40] or outcome), True)
        self._finish(ctx, f"Closed by {resolver}: {note or outcome}")
        return case

    async def undo(self, case_id: str, action_id: str, by: str = "ops.lead") -> Case:
        case, ctx = self.cases[case_id], self.ctxs[case_id]
        rec = next((a for a in case.actions if a.id == action_id), None)
        if not rec or not rec.compensation_token:
            raise ValueError("action not found or has no undo")
        u = self.gateway.undo(rec.compensation_token)
        if not u.ok:
            raise ValueError(f"undo failed: {u.error_code}")
        rec.status = "UNDONE"
        await ctx.emit("Human", "compensate", f"One-tap undo by {by}: {rec.tool}", "State restored from the gateway ledger.", {"ok": True, "tool": rec.tool})
        if ctx.rc:
            self.policy.book.record(ctx.rc.category, "undone")
            self.memory.mark_failed(case.id)
        case.status = "ESCALATED"
        self._finish(ctx, f"Action {rec.tool} undone by {by}; case returned to the human queue.")
        return case

    # ------------------------------------------------------------------ proactive: events and incidents
    async def run_scenario(self, key: str, wait: bool = False) -> Case:
        from .evals.scenarios import SCENARIOS
        sc = SCENARIOS[key]
        for fx in sc.get("fixtures", []):
            SETUPS[fx](self.world)
        for tool, n in sc.get("inject", {}).items():
            self.world.inject_failures[tool] = n
        return await self.submit(sc["party"], sc["message"], channel=sc.get("channel", "chat"), scenario=key, intent_hint=sc.get("intent_hint"), wait=wait)

    async def proactive_scan(self, wait: bool = False) -> list[str]:
        """Find merchants with recoverable holds who have NOT contacted support, and fix them first."""
        out: list[str] = []
        SETUPS["settlement_name_mismatch"](self.world)
        for s in list(self.world.settlements.values()):
            if s.status == "HELD" and s.hold_reason == "BANK_NAME_MISMATCH":
                if any(c.party_id == s.merchant_id and c.status in ("WORKING", "AWAITING_APPROVAL") for c in self.cases.values()):
                    continue
                c = await self.submit(s.merchant_id, "", channel="proactive", scenario="proactive_settlement", intent_hint="settlement_held", wait=wait)
                out.append(c.id)
        return out

    async def handle_outage(self, bank: str = "Beta Bank", failures: int = 40, healthy: int = 14) -> dict[str, Any]:
        txns = simulate_outage_txns(self.world, bank, failures, healthy)
        incident = None
        for t in sorted(txns, key=lambda x: x.created_at):
            incident = self.detector.ingest(t) or incident
        if not incident:
            return {"incident": None, "ingested": len(txns)}
        inc = incident
        self.hub.publish("incident", inc.model_dump(mode="json"))
        case = Case(party_id="ops", party_name="Resolve watchtower", party_kind="merchant", channel="event", intent="incident", category="payment_failed",
                    message=f"Incident detector fired on {bank}")
        self.cases[case.id] = case
        ctx = CaseCtx(self, case)
        self.ctxs[case.id] = ctx
        self.incident_cases.append(case.id)
        case.status = "WORKING"
        self._updated(case, "case.created")
        await ctx.emit("Incident detector", "detect", f"Incident {inc.id}: {bank} degradation", inc.suspected_cause, {"incident": inc.model_dump(mode="json")})
        await ctx.emit("Supervisor", "plan", "Switch from per-ticket to per-incident handling", "Reverse affected debits, notify every affected payer proactively, page on-call.", {})
        notified = 0
        for tid in list(inc.affected_txns):
            t = self.world.get_txn(tid)
            if not t or t.refund_id or (t.reversal and t.reversal.get("status") in ("INITIATED", "REFUNDED")) or t.credit == "CREDITED":
                continue
            a = {"txn_id": tid}
            self.gateway.call(case.id, "retry_reversal", a, token=self.gateway.issue_token(case.id, "retry_reversal", a, 0), budget=None, actor="system:watchtower")
            payer = self.world.get_party(t.payer_id)
            lang = payer.language if payer else "en"
            text = render("proactive_outage", lang, bank=bank, amount=inr(t.amount), txn=tid)
            n = {"party_id": t.payer_id, "text": text, "kind": "proactive_outage", "lang": lang, "case_id": case.id}
            r = self.gateway.call(case.id, "notify_party", n, token=self.gateway.issue_token(case.id, "notify_party", n, 0))
            case.tool_calls += 2
            if r.ok:
                notified += 1
        inc.proactive_notices = notified
        self.deflected += notified
        await ctx.emit("Executor", "tool_write", f"Auto-reversal re-triggered and {notified} payers notified in their language", "Tier 0: low-risk, reversible, rule-backed.",
                       {"notified": notified, "incident": inc.id})
        tk_args = {"case_id": case.id, "queue": "bank-ops-oncall", "priority": "P1",
                   "summary": f"{inc.id}: {inc.suspected_cause}. {notified} payers notified, reversals re-triggered. Please engage {bank}."}
        tk = self.gateway.call(case.id, "create_ticket", tk_args, token=self.gateway.issue_token(case.id, "create_ticket", tk_args, 0))
        await ctx.emit("Escalation agent", "tool_write", f"On-call paged: {tk.data.get('ticket_id')}", tk_args["summary"], {"ticket": tk.data})
        await ctx.emit("Verifier", "verify", "Incident contained", f"{notified} individual tickets avoided; incident stays OPEN until the bank recovers.", {"notified": notified})
        case.status = "RESOLVED"
        case.tier = 0
        case.summary = f"{inc.id}: one incident replaced {notified} individual tickets."
        case.duration_s = round((utcnow() - case.created_at).total_seconds(), 2)
        self._updated(case)
        self.hub.publish("incident", inc.model_dump(mode="json"))
        self.hub.publish("metrics", self.metrics())
        return {"incident": inc.model_dump(mode="json"), "notified": notified, "case_id": case.id}

    def resolve_incident(self, incident_id: str) -> None:
        self.detector.resolve(incident_id)
        inc = self.world.incidents.get(incident_id)
        if inc:
            self.hub.publish("incident", inc.model_dump(mode="json"))

    # ------------------------------------------------------------------ metrics
    def metrics(self) -> dict[str, Any]:
        cs = [c for c in self.cases.values() if c.channel != "event"]
        done = [c for c in cs if c.status in ("RESOLVED", "ESCALATED", "CLOSED", "AWAITING_CUSTOMER", "AWAITING_APPROVAL")]
        auto = [c for c in cs if c.status in ("RESOLVED", "AWAITING_CUSTOMER") and (c.tier or 0) <= 1 and not any(a.approved_by for a in c.actions)]
        esc = [c for c in cs if c.status == "ESCALATED"]
        appr = [c for c in cs if c.status == "AWAITING_APPROVAL" or any(a.approved_by for a in c.actions)]
        durs = [c.duration_s for c in auto if c.duration_s is not None]
        undone = sum(1 for c in cs for a in c.actions if a.status == "UNDONE")
        base_min = [HUMAN_BASELINE_MIN.get(c.category or "unknown", 240) for c in auto]
        saved = round(len(auto) * (HUMAN_COST_INR - AGENT_COST_INR) + self.deflected * (HUMAN_COST_INR - AGENT_COST_INR), 2)
        total = len(cs)
        return {
            "cases_total": total, "auto_resolved": len(auto), "escalated": len(esc), "awaiting_approval": len([c for c in cs if c.status == "AWAITING_APPROVAL"]),
            "approved_by_human": len([c for c in cs if any(a.approved_by for a in c.actions)]),
            "auto_resolution_rate": round(len(auto) / total, 3) if total else 0.0,
            "median_time_s": round(statistics.median(durs), 2) if durs else None,
            "modeled_human_baseline_min": round(statistics.median(base_min), 0) if base_min else None,
            "wrong_actions": undone, "wrong_action_rate": round(undone / max(1, len([a for c in cs for a in c.actions if a.status in ("EXECUTED", "UNDONE")])), 4),
            "tool_calls": sum(c.tool_calls for c in cs), "llm_calls": sum(c.llm_calls for c in cs),
            "incidents_open": len([i for i in self.world.incidents.values() if i.status == "OPEN"]), "tickets_deflected": self.deflected,
            "modeled_savings_inr": saved, "llm": self.llm.name,
            "pending": len([c for c in cs if c.status in ("WORKING", "OPEN")]),
        }
