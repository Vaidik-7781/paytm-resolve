"""Split conformal prediction for the intent model: a distribution-free coverage guarantee under exchangeability.

Calibrate on held-out *validation phrasing* (never training phrases). The prediction set contains every intent whose
calibrated probability clears the threshold; a set that is not a singleton means "ambiguous" and goes to a human.
We also report REAL empirical coverage on a different unseen partition, which is lower when phrasing shifts. That
honesty is the point: it quantifies distribution shift instead of hiding it.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from .corpus import generate
from .intent_model import get_model, softmax

_Q: dict[float, float] = {}


def _probs(texts: list[str]) -> np.ndarray:
    m = get_model()
    return np.stack([softmax(m.logits(t) / m.T) for t in texts])


def fit(alpha: float = 0.1) -> float:
    m = get_model()
    cal = generate(120, seed=555, part=1, noise=0.15) + generate(60, seed=556, part=1, noise=0.0, asr=0.3)
    P = _probs([t for t, _, _ in cal])
    y = np.array([m.labels.index(i) for _, i, _ in cal])
    scores = 1.0 - P[np.arange(len(y)), y]
    n = len(scores)
    q = float(np.quantile(scores, min(1.0, math.ceil((n + 1) * (1 - alpha)) / n), method="higher"))
    _Q[alpha] = q
    return q


def prediction_set(text: str, alpha: float = 0.1) -> list[str]:
    q = _Q.get(alpha) or fit(alpha)
    m = get_model()
    p = softmax(m.logits(text) / m.T)
    return [m.labels[i] for i in range(len(p)) if p[i] >= 1.0 - q] or [m.labels[int(np.argmax(p))]]


def evaluate(alpha: float = 0.1) -> dict[str, Any]:
    m = get_model()
    q = fit(alpha)
    out = {"alpha": alpha, "target_coverage": 1 - alpha, "threshold": round(q, 4), "slices": []}
    for name, kw in (("unseen phrasing, clean", dict(noise=0.0)), ("unseen phrasing, 25% typos", dict(noise=0.25)), ("unseen phrasing, ASR-style errors", dict(noise=0.0, asr=0.4))):
        rows = generate(150, seed=909, part=2, **kw)
        P = _probs([t for t, _, _ in rows])
        y = np.array([m.labels.index(i) for _, i, _ in rows])
        sets = [[j for j in range(P.shape[1]) if P[i, j] >= 1 - q] or [int(np.argmax(P[i]))] for i in range(len(rows))]
        cov = float(np.mean([y[i] in sets[i] for i in range(len(rows))]))
        single = [i for i in range(len(rows)) if len(sets[i]) == 1]
        acc_single = float(np.mean([sets[i][0] == y[i] for i in single])) if single else 0.0
        out["slices"].append({"slice": name, "coverage": round(cov, 3), "avg_set_size": round(float(np.mean([len(s) for s in sets])), 2),
                              "singleton_rate": round(len(single) / len(rows), 3), "accuracy_when_singleton": round(acc_single, 3)})
    return out


def selective(n: int = 150) -> dict[str, Any]:
    """Risk-coverage: accuracy among the most confident c% of predictions; AURC (lower is better)."""
    m = get_model()
    res = {}
    for name, kw in (("ASR-style errors", dict(asr=0.4)), ("25% typos", dict(noise=0.25))):
        rows = generate(n, seed=1010, part=2, **kw)
        P = _probs([t for t, _, _ in rows])
        y = np.array([m.labels.index(i) for _, i, _ in rows])
        conf, pred = P.max(1), P.argmax(1)
        order = np.argsort(-conf)
        correct = (pred == y)[order]
        curve = []
        for cov in (1.0, 0.9, 0.8, 0.7, 0.5):
            k = max(1, int(cov * len(order)))
            curve.append({"coverage": cov, "accuracy": round(float(correct[:k].mean()), 3)})
        risks = [1 - correct[:k].mean() for k in range(1, len(correct) + 1)]
        res[name] = {"curve": curve, "aurc": round(float(np.mean(risks)), 4)}
    return res
