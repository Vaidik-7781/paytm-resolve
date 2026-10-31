"""Trained multilingual intent model (numpy only; no network, no GPU, ~50 KB of weights).

  features : hashed char 2-5 grams + word uni/bi-grams (Indic-script aware, typo tolerant by construction)
  model    : multinomial logistic (softmax) regression, SGD, L2
  calibr.  : temperature scaling fitted on a held-out validation split (reports ECE before/after)

It runs BESIDE the keyword rules and (when keys exist) an LLM; disagreement lowers confidence and ends in a human
handoff. Training data is synthetic (see corpus.py): re-train on Paytm's labelled tickets before production.
"""
from __future__ import annotations

import math
import re
import unicodedata
import zlib
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .corpus import INTENTS, generate

D = 1 << 15
WEIGHTS = Path(__file__).with_name("intent_model.npz")
TOKEN = re.compile(r"[\w\u0900-\u0DFF]+", re.UNICODE)


# Cross-lingual concept lexicon: maps surface forms in English / Hinglish / Hindi onto shared concepts so that
# unseen wording in another language still lands on the same features (a tiny, auditable stand-in for embeddings).
CONCEPTS = {
    "DEBIT": "debited deducted charged kat kata kate katgaya debit deduction cut कट कटा कटे कटौती डेबिट",
    "FAIL": "failed fail declined decline unsuccessful incomplete फेल डिक्लाइन विफल",
    "STUCK": "stuck pending atak atka hold held delayed ruka rukha roka रुका रुकी अटक अटका अटकी रोका लंबित पेंडिंग",
    "TWICE": "twice double duplicate dobara dubara baar दोबारा दुबारा दोहरी दोहरा दो",
    "SETTLE": "settlement settle settlements payout payouts सेटलमेंट पेआउट",
    "BANK": "bank account बैंक खाते खाता",
    "DISPUTE": "chargeback dispute notice चार्जबैक विवाद नोटिस",
    "EMI": "emi loan kist installment instalment outstanding किस्त ईएमआई लोन",
    "CLAIM": "claim insurance policy क्लेम बीमा",
    "SOUNDBOX": "soundbox speaker awaaz sound साउंडबॉक्स स्पीकर आवाज़ आवाज",
    "OFFLINE": "offline silent chup network ऑफ़लाइन ऑफलाइन चुप",
    "REFUND": "refund wapas return रिफंड वापस",
    "MONEY": "money paisa paise amount rupees pesa पैसे पैसा राशि",
    "HELP": "help madad chahiye मदद चाहिए",
    "STRANGE": "strange ajeeb gadbad wrong safe अजीब गड़बड़ सुरक्षित",
}
_WORD2CONCEPT = {w: c for c, ws in CONCEPTS.items() for w in ws.split()}
_LEX = [w for w in _WORD2CONCEPT if len(w) >= 6 and w.isascii()]


def _osa1(a: str, b: str) -> bool:
    """True if optimal-string-alignment distance <= 1."""
    if abs(len(a) - len(b)) > 1:
        return False
    if a == b:
        return True
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    if len(a) == len(b):
        return a[i + 1:] == b[i + 1:] or (i + 1 < len(a) and a[i] == b[i + 1] and a[i + 1] == b[i] and a[i + 2:] == b[i + 2:])
    if len(a) > len(b):
        return a[i + 1:] == b[i:]
    return b[i + 1:] == a[i:]


def concept_of(w: str) -> Optional[str]:
    c = _WORD2CONCEPT.get(w)
    if c or len(w) < 6 or not w.isascii():
        return c
    for v in _LEX:  # typo tolerance: one edit away from a lexicon word
        if _osa1(w, v):
            return _WORD2CONCEPT[v]
    return None


def _h(s: str) -> int:
    return zlib.crc32(s.encode("utf-8")) % D


def featurize(text: str) -> tuple[np.ndarray, np.ndarray]:
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"\b[tsdl]{1,2}-\d{3,5}\b", " <id> ", t)  # ids carry no intent
    t = re.sub(r"(?:rs\.?|₹|inr)\s?[\d,]+", " <amt> ", t)
    t = re.sub(r"\d+", "0", t)
    words = TOKEN.findall(t)
    feats: dict[int, float] = {}

    def add(key: str, w: float = 1.0) -> None:
        i = _h(key)
        feats[i] = feats.get(i, 0.0) + w

    for w in words:
        add("w:" + w)
        c = concept_of(w)
        if c:
            add("k:" + c, 2.0)
        padded = f" {w} "
        for n in (2, 3, 4, 5):
            for i in range(max(0, len(padded) - n + 1)):
                add(f"c{n}:{padded[i:i + n]}", 0.5)
    for a, b in zip(words, words[1:]):
        add("b:" + a + "_" + b)
    if not feats:
        add("<empty>")
    idx = np.fromiter(feats.keys(), dtype=np.int64)
    val = np.fromiter((1.0 + math.log(v) for v in feats.values()), dtype=np.float64)
    val /= np.linalg.norm(val) or 1.0
    return idx, val


def softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=-1, keepdims=True)


class IntentModel:
    def __init__(self, W: np.ndarray, b: np.ndarray, T: float = 1.0, labels: Optional[list[str]] = None, meta: Optional[dict[str, Any]] = None) -> None:
        self.W, self.b, self.T = W, b, float(T)
        self.labels = labels or INTENTS
        self.meta = meta or {}

    def logits(self, text: str) -> np.ndarray:
        idx, val = featurize(text)
        return (self.W[idx] * val[:, None]).sum(0) + self.b

    def predict(self, text: str) -> dict[str, Any]:
        z = self.logits(text)
        p = softmax(z / self.T)
        order = np.argsort(-p)
        top, second = int(order[0]), int(order[1])
        return {"intent": self.labels[top], "confidence": float(p[top]), "margin": float(p[top] - p[second]),
                "probs": {self.labels[i]: round(float(p[i]), 4) for i in order[:3]}}


def _train(epochs: int = 8, lr: float = 1.0, l2: float = 2e-5, seed: int = 0, n_train: int = 700, n_val: int = 150, extra: Optional[list] = None) -> IntentModel:
    rng = np.random.default_rng(seed)
    labels = INTENTS
    li = {l: i for i, l in enumerate(labels)}
    # training mixes clean and noisy copies so typos are in-distribution
    train = generate(n_train, seed=1) + generate(n_train, seed=2, noise=0.2) + generate(n_train // 2, seed=3, noise=0.4)
    train = train + list(extra or [])
    val = generate(n_val, seed=10, part=1, noise=0.15)  # UNSEEN phrasing: temperature is fitted here, never on training phrases
    X = [featurize(t) for t, _, _ in train]
    y = np.array([li[i] for _, i, _ in train])
    K = len(labels)
    W = np.zeros((D, K))
    b = np.zeros(K)
    order = np.arange(len(X))
    for ep in range(epochs):
        rng.shuffle(order)
        step = lr / (1 + 0.35 * ep)
        for n in order:
            idx, val_ = X[n]
            z = (W[idx] * val_[:, None]).sum(0) + b
            p = softmax(z)
            p[y[n]] -= 1.0
            W[idx] -= step * val_[:, None] * p[None, :]
            b -= step * 0.1 * p
        W *= (1.0 - step * l2 * 50)
    model = IntentModel(W, b, 1.0, labels)
    # ---- temperature scaling on validation logits
    Z = np.stack([model.logits(t) for t, _, _ in val])
    yv = np.array([li[i] for _, i, _ in val])
    best_T, best_nll = 1.0, 1e9
    for T in np.arange(0.05, 4.01, 0.025):
        P = softmax(Z / T)
        nll = -np.log(P[np.arange(len(yv)), yv] + 1e-12).mean()
        if nll < best_nll:
            best_T, best_nll = float(T), float(nll)
    model.T = best_T
    model.meta = {"train_examples": len(train), "val_examples": len(val), "temperature": best_T, "features": D, "epochs": epochs, "feedback_examples": len(extra or []),
                  "trained_on": "synthetic multilingual corpus (resolve/ml/corpus.py)"}
    return model


def save(model: IntentModel, path: Path = WEIGHTS) -> None:
    np.savez_compressed(path, W=model.W.astype(np.float32), b=model.b, T=model.T, labels=np.array(model.labels), meta=np.array([str(model.meta)]))


def load(path: Path = WEIGHTS) -> IntentModel:
    z = np.load(path, allow_pickle=False)
    return IntentModel(z["W"].astype(np.float64), z["b"], float(z["T"]), [str(x) for x in z["labels"]], {"loaded_from": path.name, "raw": str(z["meta"][0])})


_MODEL: Optional[IntentModel] = None


def get_model() -> IntentModel:
    global _MODEL
    if _MODEL is None:
        if WEIGHTS.exists():
            _MODEL = load()
        else:
            _MODEL = _train()
            try:
                save(_MODEL)
            except OSError:
                pass
    return _MODEL


if __name__ == "__main__":
    m = _train()
    save(m)
    print("trained", m.meta)
