"""Agentic investigator for novel cases (no playbook matched).

A bounded, READ-ONLY tool-using loop. With Claude it uses native tool use; with Sarvam, OpenAI-style tool calls;
offline, a deterministic triage. Safety properties that hold in every mode:
  * only allow-listed read tools; every call goes through the gateway, which enforces per-requester authorization
  * at most 6 steps; tool results are data, never instructions
  * the output is a HYPOTHESIS plus recommendations for a human: it can never create an executable action
"""
from __future__ import annotations

from typing import Any

from .specialists import Findings

READ_ALLOW = ["list_recent_txns", "get_txn", "get_upi_status", "list_settlements", "get_settlement", "get_kyc", "list_disputes", "get_dispute",
              "list_loans", "get_loan", "list_claims", "get_claim", "list_devices", "get_device", "search_policy"]


async def investigate(ctx, intent: str) -> Findings:
    eng, case = ctx.engine, ctx.case
    tools = [t.schema() for t in eng.gateway.tools.values() if t.scope == "read" and t.name in READ_ALLOW]
    brief = {"party_id": case.party_id, "party_kind": case.party_kind, "language": case.language, "intent_guess": intent,
             "message": (ctx.guard.masked_text or ctx.guard.clean_text or "")[:600], "known_root_causes": list(eng_known())}

    async def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in READ_ALLOW:
            return {"error": "tool not allowed for investigation"}
        res = await ctx.read("Investigator", name, args)
        return res.data if res.ok else {"error": res.error_code}

    out = await eng.llm.investigate(brief, tools, call_tool, 6)
    if eng.llm.name != "offline-deterministic":
        case.llm_calls += 1
    ctx.facts["investigation"] = out
    await ctx.emit("Investigator", "investigation", f"Hypothesis: {out['hypothesis']} ({out['confidence']:.2f}) after {out['steps']} read-only step(s)",
                   out["reasoning"], {"investigation": out})
    summary = f"{out['hypothesis']} ({out['confidence']:.2f}): {out['reasoning']}"
    return Findings("Investigator", summary, {"investigation": out})


def eng_known() -> list[str]:
    from .reasoning import KNOWN
    return KNOWN
