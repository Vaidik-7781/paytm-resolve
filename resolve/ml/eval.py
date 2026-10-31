"""Honest evaluation of the intent stack on phrases it never saw: ML vs keyword rules vs ensemble, per language,
under typo noise, with calibration (ECE) before and after temperature scaling."""
from __future__ import annotations

from typing import Any

import numpy as np

from ..llm.offline import classify as rules_classify
from .corpus import LANGS, generate
from .ensemble import combine
from .intent_model import get_model, softmax


def ece(conf: np.ndarray, correct: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    out = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (conf > lo) & (conf <= hi)
        if m.any():
            out += m.mean() * abs(correct[m].mean() - conf[m].mean())
    return float(out)


def run(n: int = 200) -> dict[str, Any]:
    model = get_model()
    result: dict[str, Any] = {"model": model.meta, "temperature": model.T, "n_per_intent": n, "buckets": []}
    for noise in (0.0, 0.25, 0.5):
        rows = generate(n, seed=1234, part=2, noise=noise)
        ml_ok, rule_ok, ens_ok = [], [], []
        misroute, deferred = 0, 0
        conf, correct_ml = [], []
        raw_conf = []
        by_lang = {l: [0, 0, 0, 0] for l in LANGS}
        for text, intent, lang in rows:
            z = model.logits(text)
            p = softmax(z / model.T)
            p_raw = softmax(z)
            pred = model.labels[int(np.argmax(p))]
            r = rules_classify(text)["intent"]
            e = combine(text)["intent"]
            ml_ok.append(pred == intent); rule_ok.append(r == intent); ens_ok.append(e == intent)
            misroute += (e != intent and e != 'unknown'); deferred += (e == 'unknown' and intent != 'unknown')
            conf.append(p.max()); raw_conf.append(p_raw.max()); correct_ml.append(pred == intent)
            b = by_lang[lang]; b[0] += 1; b[1] += pred == intent; b[2] += r == intent; b[3] += e == intent
        c, rc, ok = np.array(conf), np.array(raw_conf), np.array(correct_ml, dtype=float)
        result["buckets"].append({
            "noise": noise, "n": len(rows), "ml_accuracy": round(float(np.mean(ml_ok)), 3), "rules_accuracy": round(float(np.mean(rule_ok)), 3),
            "ensemble_accuracy": round(float(np.mean(ens_ok)), 3), "ensemble_misroute_rate": round(misroute / len(rows), 4), "ensemble_deferred_rate": round(deferred / len(rows), 4), "ece_calibrated": round(ece(c, ok), 4), "ece_uncalibrated": round(ece(rc, ok), 4),
            "by_language": {l: {"n": v[0], "ml": round(v[1] / v[0], 3), "rules": round(v[2] / v[0], 3), "ensemble": round(v[3] / v[0], 3)} for l, v in by_lang.items()},
        })
    from ..evals.scenarios import SCENARIOS
    cat = [(sc["message"], sc["expect"]["intent"]) for sc in SCENARIOS.values() if sc["expect"].get("intent")]
    result["catalog"] = {"n": len(cat), "ml": round(sum(get_model().predict(t)["intent"] == i for t, i in cat) / len(cat), 3),
                         "rules": round(sum(rules_classify(t)["intent"] == i for t, i in cat) / len(cat), 3),
                         "ensemble": round(sum(combine(t)["intent"] == i for t, i in cat) / len(cat), 3)}
    result["note"] = "Synthetic held-out phrases (unseen in training). Re-measure on Paytm's labelled tickets before production."
    return result
