"""Tamper-evident decision trail: every trace event is hash-chained (SHA-256), the head is sealed when the case
finishes, and the audit pack can be re-verified later. Editing, deleting or re-ordering any event breaks the chain."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import Case


def _canon(ev: dict[str, Any]) -> str:
    keep = {k: ev.get(k) for k in ("id", "ts", "agent", "kind", "title", "detail")}
    keep["data"] = ev.get("data", {})
    return json.dumps(keep, sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":"))


def chain(events: list[dict[str, Any]]) -> list[str]:
    h, out = "GENESIS", []
    for ev in events:
        h = hashlib.sha256((h + _canon(ev)).encode()).hexdigest()
        out.append(h)
    return out


def seal(case: Case) -> str:
    evs = [e.model_dump(mode="json") for e in case.trace]
    case.audit_head = chain(evs)[-1] if evs else None
    return case.audit_head or ""


def verify(events: list[dict[str, Any]], head: str) -> bool:
    c = chain(events)
    return bool(c) and c[-1] == head


def pack(case: Case, engine: Any) -> dict[str, Any]:
    evs = [e.model_dump(mode="json") for e in case.trace]
    hashes = chain(evs)
    led = [e for e in engine.gateway.ledger.values() if e["case_id"] == case.id]
    from .ml.intent_model import get_model
    im = get_model()
    models = {"llm": engine.llm.name, "intent_model": {"temperature": round(im.T, 3), "weights": im.meta.get("loaded_from", "in-memory (retrained)")}}
    return {
        "case_id": case.id, "party": {"id": case.party_id, "kind": case.party_kind}, "language": case.language, "channel": case.channel, "status": case.status, "tier": case.tier,
        "policy_version": engine.policy.version, "models": models,
        "decision": {"intent": case.intent, "root_cause": case.root_cause.model_dump(mode="json") if case.root_cause else None,
                     "critic": case.critic.model_dump(mode="json") if case.critic else None, "policy": case.policy.model_dump(mode="json") if case.policy else None},
        "actions": [a.model_dump(mode="json") for a in case.actions], "gateway_ledger": led, "approval": case.approval, "flags": case.flags,
        "trace": [{"i": i, "hash": hashes[i], **{k: ev[k] for k in ("ts", "agent", "kind", "title", "detail")}} for i, ev in enumerate(evs)],
        "chain_head": hashes[-1] if hashes else None, "sealed_head": case.audit_head, "chain_valid": bool(hashes) and (case.audit_head in (None, hashes[-1])),
        "note": "Each event hash = SHA256(previous hash + canonical event). Any edit, deletion or reorder changes every later hash and the head.",
    }


def to_markdown(p: dict[str, Any]) -> str:
    d = p["decision"]
    lines = [f"# Audit pack: {p['case_id']}", "", f"- Requester: {p['party']['id']} ({p['party']['kind']}), language {p['language']}, channel {p['channel']}",
             f"- Outcome: {p['status']} at Tier {p['tier']} under {p['policy_version']}; models: {p['models']}",
             f"- Chain head: `{p['chain_head']}` (valid: {p['chain_valid']})", "", "## Decision",
             f"- Intent: {d['intent']}", f"- Root cause: {(d['root_cause'] or {}).get('code')} (confidence {(d['root_cause'] or {}).get('confidence')}) citing {(d['root_cause'] or {}).get('citations')}",
             f"- Critic: {(d['critic'] or {}).get('verdict')}", "- Policy reasons:"]
    lines += [f"  - {r['rule']}: {r['detail']}" for r in (d["policy"] or {}).get("reasons", [])]
    lines += ["", "## Actions"] + [f"- {a['tool']} {a['status']} (tier {a['tier']}, approved by {a['approved_by'] or 'n/a'})" for a in p["actions"]]
    lines += ["", "## Trace (hash-chained)"] + [f"{t['i']:>3} `{t['hash'][:10]}` {t['agent']}: {t['title']}" for t in p["trace"]]
    return "\n".join(lines)
