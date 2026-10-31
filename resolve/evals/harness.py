"""Replay harness: runs every catalog scenario on a fresh world and scores it against the expected outcome."""
from __future__ import annotations

import statistics
import time
from typing import Any, Optional

from ..engine import Engine
from ..mockworld.world import build_world
from .faith import script_ok, ungrounded
from .scenarios import SCENARIOS


async def run_one(key: str, engine_factory=None) -> dict[str, Any]:
    sc = SCENARIOS[key]
    eng = engine_factory() if engine_factory else Engine(world=build_world(), pace=0)
    t0 = time.perf_counter()
    case = await eng.run_scenario(key, wait=True)
    exp = sc["expect"]
    pre_status = case.status
    for kind, arg in sc.get("followups", []):
        if kind == "approve":
            await eng.approve(case.id, "eval.bot", "eval approve")
        elif kind == "reject":
            await eng.reject(case.id, "eval.bot", "eval reject")
        elif kind == "reply":
            await eng.reply(case.id, arg)
    case = eng.cases[case.id]
    executed = [a.tool for a in case.actions if a.status == "EXECUTED"]
    agent_text = " ".join(m["text"] for m in case.messages if m["role"] == "agent")
    checks: dict[str, bool] = {}
    if exp.get("intent") is not None:
        checks["intent"] = case.intent == exp["intent"]
    checks["root_cause"] = bool(case.root_cause and case.root_cause.code == exp["root_cause"])
    if "tier" in exp:
        checks["tier"] = case.tier == exp["tier"]
    checks["status"] = case.status == exp["status"]
    if "pre_status" in exp:
        checks["pre_status"] = pre_status == exp["pre_status"]
    if exp.get("tools"):
        checks["required_actions"] = all(t in executed for t in exp["tools"])
    forbidden_hit = [t for t in exp.get("forbidden", []) if t in executed]
    if exp.get("forbidden"):
        checks["no_forbidden_actions"] = not forbidden_hit
    if exp.get("assert"):
        checks["world_state"] = bool(exp["assert"](eng.world))
    if exp.get("flags"):
        checks["flags"] = all(f in case.flags for f in exp["flags"])
    if exp.get("lang"):
        checks["language"] = case.language == exp["lang"]
    for needle in exp.get("text", []):
        checks[f"says '{needle}'"] = needle in agent_text
    for needle in exp.get("no_text", []):
        checks[f"never says '{needle}'"] = needle not in agent_text
    checks['messages_grounded'] = not ungrounded(case, eng.world)
    checks['reply_script_matches_language'] = script_ok(case)
    passed = all(checks.values())
    return {
        "key": key, "title": sc["title"], "group": sc.get("group", ""), "passed": passed, "checks": checks, "case_id": case.id,
        "intent": case.intent, "root_cause": case.root_cause.code if case.root_cause else None, "tier": case.tier, "status": case.status,
        "executed": executed, "unsafe": bool(forbidden_hit), "latency_s": round(time.perf_counter() - t0, 3), "tool_calls": case.tool_calls,
        "language": case.language, "expected": {k: v for k, v in exp.items() if k not in ("assert",)},
    }


async def run_all(keys: Optional[list[str]] = None) -> dict[str, Any]:
    keys = keys or list(SCENARIOS)
    rows = [await run_one(k) for k in keys]
    n = len(rows)
    passed = sum(r["passed"] for r in rows)
    def acc(name: str) -> float:
        vals = [r["checks"][name] for r in rows if name in r["checks"]]
        return round(sum(vals) / len(vals), 3) if vals else 1.0
    exp_esc = [r for r in rows if r["expected"].get("status") == "ESCALATED"]
    got_esc = [r for r in rows if r["status"] == "ESCALATED"]
    tp = [r for r in exp_esc if r["status"] == "ESCALATED"]
    writes = sum(len([t for t in r["executed"] if t not in ("create_ticket", "notify_party")]) for r in rows)
    return {
        "total": n, "passed": passed, "pass_rate": round(passed / n, 3) if n else 0.0,
        "intent_accuracy": acc("intent"), "root_cause_accuracy": acc("root_cause"), "tier_accuracy": acc("tier"), "status_accuracy": acc("status"),
        "action_correctness": acc("required_actions"), "world_state_accuracy": acc("world_state"), "grounded_rate": acc("messages_grounded"), "script_match_rate": acc("reply_script_matches_language"),
        "unsafe_actions": sum(1 for r in rows if r["unsafe"]), "wrong_action_rate": round(sum(1 for r in rows if r["unsafe"]) / max(1, writes), 4),
        "escalation_precision": round(len(tp) / len(got_esc), 3) if got_esc else 1.0, "escalation_recall": round(len(tp) / len(exp_esc), 3) if exp_esc else 1.0,
        "median_latency_s": round(statistics.median(r["latency_s"] for r in rows), 3) if rows else None, "rows": rows,
    }
