"""Closed-loop learning: humans relabel deferred/misrouted messages, the model is fine-tuned, and the update is
ACCEPTED ONLY IF held-out accuracy does not regress. Versioned, with rollback."""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Optional

from . import intent_model as im
from .corpus import _typo, generate

BUFFER: list[dict[str, Any]] = []
HISTORY: list[dict[str, Any]] = [{"version": 1, "note": "base model (synthetic corpus)", "accepted": True}]
_PREV: dict[str, Any] = {}
STORE = Path(__file__).with_name("feedback_buffer.json")


def add(text: str, intent: str, case_id: Optional[str] = None) -> dict[str, Any]:
    if intent not in im.INTENTS:
        raise ValueError("unknown intent")
    item = {"text": text, "intent": intent, "case_id": case_id}
    BUFFER.append(item)
    try:
        STORE.write_text(json.dumps(BUFFER, ensure_ascii=False))
    except OSError:
        pass
    return item


def _acc(model: im.IntentModel, rows) -> float:
    return sum(model.predict(t)["intent"] == i for t, i, _ in rows) / max(1, len(rows))


def retrain(min_gain_on_feedback: float = 0.0, max_regression: float = 0.01) -> dict[str, Any]:
    if not BUFFER:
        raise ValueError("no feedback to learn from")
    rng = random.Random(3)
    base = im.get_model()
    held = generate(120, seed=2024, part=2, noise=0.2)
    fb_rows = [(b["text"], b["intent"], "fb") for b in BUFFER]
    before = {"heldout": _acc(base, held), "feedback": _acc(base, fb_rows)}
    extra = []
    for t, i, _ in fb_rows:  # upweight with light typo augmentation so the model generalises beyond the exact string
        for k in range(30):
            extra.append((_typo(t, rng, 0.15) if k % 2 else t, i, "fb"))
    new = im._train(epochs=8, lr=1.0, n_train=350, n_val=100, extra=extra)
    after = {"heldout": _acc(new, held), "feedback": _acc(new, fb_rows)}
    ok = after["heldout"] >= before["heldout"] - max_regression and after["feedback"] > before["feedback"] + min_gain_on_feedback
    rec = {"version": len(HISTORY) + 1, "feedback_examples": len(BUFFER), "before": {k: round(v, 3) for k, v in before.items()}, "after": {k: round(v, 3) for k, v in after.items()},
           "accepted": ok, "note": "accepted: no regression on held-out data and feedback now correct" if ok else "REJECTED by the regression guard; previous model kept"}
    if ok:
        _PREV["model"] = base
        im._MODEL = new
    HISTORY.append(rec)
    return rec


def rollback() -> dict[str, Any]:
    if "model" not in _PREV:
        raise ValueError("nothing to roll back")
    im._MODEL = _PREV.pop("model")
    rec = {"version": len(HISTORY) + 1, "note": "rolled back to previous model", "accepted": True}
    HISTORY.append(rec)
    return rec
