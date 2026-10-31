"""Faithfulness checks on customer-facing messages: every id and amount must be traceable to the systems of record
(no hallucinated numbers), and the script must match the reply language."""
from __future__ import annotations

import json
import re
from typing import Any

from ..models import Case

ID = re.compile(r"\b[A-Z]{1,3}-\d{3,6}\b")
AMT = re.compile(r"₹\s?([\d,]+(?:\.\d+)?)")
DEV = re.compile(r"[\u0900-\u097F]")


def _blob(case: Case, world: Any) -> str:
    parts = [case.message, json.dumps(case.root_cause.signals, default=str) if case.root_cause else "", json.dumps([a.result for a in case.actions], default=str),
             json.dumps([a.args for a in case.actions], default=str), json.dumps([t.data for t in case.trace if t.kind in ("finding", "tool_read")], default=str)]
    return " ".join(parts).replace(",", "")


def ungrounded(case: Case, world: Any) -> list[str]:
    blob = _blob(case, world)
    bad = []
    for m in case.messages:
        if m["role"] != "agent" or m.get("kind") == "ack":
            continue
        for tok in ID.findall(m["text"]):
            if tok not in blob:
                bad.append(tok)
        for amt in AMT.findall(m["text"]):
            a = amt.replace(",", "").rstrip(".")
            if a not in blob and (a + ".0") not in blob:
                bad.append("₹" + amt)
    return bad


def script_ok(case: Case) -> bool:
    for m in case.messages:
        if m["role"] != "agent" or m.get("kind") == "ack":
            continue
        has_dev = bool(DEV.search(m["text"]))
        if case.language == "hi" and not has_dev:
            return False
        if case.language in ("en", "hinglish") and has_dev:
            return False
    return True
