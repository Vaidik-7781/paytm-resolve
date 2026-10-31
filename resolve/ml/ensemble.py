"""Intent ensemble: trained model + keyword rules (+ optional LLM vote). Disagreement lowers confidence and ends
in a human handoff instead of a guess."""
from __future__ import annotations

from typing import Any, Optional

from ..llm.offline import classify as rules_classify, extract_entities
from .intent_model import get_model


def combine(text: str, llm_vote: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    ml = get_model().predict(text)
    rules = rules_classify(text)
    votes = {"ml": ml["intent"], "rules": rules["intent"]}
    if llm_vote:
        votes["llm"] = llm_vote["intent"]
    tally: dict[str, int] = {}
    for v in votes.values():
        tally[v] = tally.get(v, 0) + 1
    top, n = max(tally.items(), key=lambda kv: kv[1])
    if n == len(votes):
        intent, conf, source = top, min(0.99, max(ml["confidence"], rules["confidence"]) + 0.02), "agree"
    elif llm_vote and n >= 2:
        intent, conf, source = top, min(0.95, 0.8 + 0.05 * n), "majority"
    elif ml["intent"] != "unknown" and rules["intent"] == "unknown" and ml["confidence"] >= 0.8:
        intent, conf, source = ml["intent"], ml["confidence"] * 0.95, "ml-only"  # keyword rules blind (typos, new phrasing)
    elif rules["intent"] != "unknown" and ml["intent"] == "unknown" and rules["confidence"] >= 0.85:
        intent, conf, source = rules["intent"], rules["confidence"] * 0.9, "rules-only"
    else:
        intent, conf, source = "unknown", 0.4, "disagree"
    return {"intent": intent, "confidence": round(conf, 3), "entities": extract_entities(text), "source": source, "votes": votes,
            "ml": {"intent": ml["intent"], "confidence": round(ml["confidence"], 3), "top3": ml["probs"]}, "rules": {"intent": rules["intent"], "confidence": rules["confidence"]}}
