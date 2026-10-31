"""Latency / throughput benchmark of the local stack + an explicit cost model, and incident-detector evaluation."""
from __future__ import annotations

import asyncio
import random
import statistics
import time
from collections import deque
from typing import Any

from ..engine import Engine
from ..guard import inspect
from ..ml.corpus import generate
from ..ml.ensemble import combine
from ..mockworld.world import build_world
from ..models import Party


def _pcts(xs: list[float]) -> dict[str, float]:
    xs = sorted(xs)
    return {"p50": round(xs[len(xs) // 2], 3), "p95": round(xs[int(len(xs) * 0.95) - 1], 3), "max": round(xs[-1], 3)}


async def latency_and_throughput(n: int = 300, concurrency: int = 50) -> dict[str, Any]:
    texts = [t for t, _, _ in generate(40, seed=31, part=2, noise=0.1)][:200]
    g, m = [], []
    for t in texts:
        t0 = time.perf_counter(); gi = inspect(t); g.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter(); combine(gi.masked_text or t); m.append((time.perf_counter() - t0) * 1000)
    eng = Engine(world=build_world(only=[]), pace=0)
    w = eng.world
    from ..models import Txn
    from ..mockworld.world import ago
    for i in range(n):
        w.parties[f"b{i}"] = Party(id=f"b{i}", kind="customer", name=f"B{i}", language="hinglish")
        w.txns[f"TB-{i}"] = Txn(id=f"TB-{i}", payer_id=f"b{i}", merchant_id="m_x", amount=999, bank="Alpha Bank", debit="DEBITED", credit="FAILED", status="FAILED", created_at=ago(30))
    t0 = time.perf_counter()
    sem = asyncio.Semaphore(concurrency)

    async def one(i: int):
        async with sem:
            await eng.submit(f"b{i}", "mera payment fail ho gaya, paisa kat gaya", wait=True)
    await asyncio.gather(*[one(i) for i in range(n)])
    wall = time.perf_counter() - t0
    dur = [c.duration_s or 0 for c in eng.cases.values()]
    ok = sum(1 for c in eng.cases.values() if c.status == "RESOLVED")
    return {"cases": n, "concurrency": concurrency, "wall_s": round(wall, 2), "throughput_cases_per_s": round(n / wall, 1), "resolved": ok, "case_latency_s": _pcts(dur),
            "guard_ms": _pcts(g), "intent_ensemble_ms": _pcts(m), "note": "Single Python process, mock systems, no network. Remote LLM/Sarvam calls add their own latency; the local stack is the floor."}


def cost_model() -> dict[str, Any]:
    """ILLUSTRATIVE per-case cost. Token counts are assumptions; replace prices with your contracted rates."""
    A = {"claude_fast_intent": (350, 40), "claude_reasoning_investigator": (3500, 500), "sarvam_intent": (350, 40), "sarvam_compose": (250, 80)}
    price = {"in_per_mtok_inr": 100.0, "out_per_mtok_inr": 400.0}  # placeholder blended INR per million tokens
    def c(*keys):
        tin = sum(A[k][0] for k in keys); tout = sum(A[k][1] for k in keys)
        return round(tin / 1e6 * price["in_per_mtok_inr"] + tout / 1e6 * price["out_per_mtok_inr"], 3)
    return {"assumed_prices": price, "tokens_assumed": A, "per_case_inr": {"local only (no API keys)": 0.0, "routine case, Sarvam vote + reply": c("sarvam_intent", "sarvam_compose"),
            "routine case, Claude vote + Sarvam reply": c("claude_fast_intent", "sarvam_compose"), "novel case with agentic investigation": c("claude_fast_intent", "claude_reasoning_investigator", "sarvam_compose")},
            "note": "Routine cases need no reasoning model: the local stack decides; LLM calls are votes and wording. Only novel cases pay for investigation."}


def detector_eval(trials: int = 200, seed: int = 4) -> dict[str, Any]:
    """Window-threshold detector (production prototype) vs one-sided CUSUM, on simulated bank streams.
    Baseline failure prob 4%; incident raises it to 45% at t0. Metrics: detection delay (events) and false alarms per 10k events."""
    rng = random.Random(seed)
    p0, p1 = 0.04, 0.45

    def thr_detect(stream):
        win = deque(maxlen=60); fails = 0
        for i, f in enumerate(stream):
            if len(win) == win.maxlen:
                fails -= win[0]
            win.append(f); fails += f
            if fails >= 12 and fails / len(win) >= 0.35:
                return i
        return None

    def cusum_detect(stream, k=0.12, h=4.0):
        s = 0.0
        for i, f in enumerate(stream):
            s = max(0.0, s + (f - p0 - k))
            if s > h:
                return i
        return None

    res = {}
    for name, det in (("window threshold (12 fails & 35%)", thr_detect), ("CUSUM (k=0.12, h=4)", cusum_detect)):
        delays, miss = [], 0
        for _ in range(trials):
            t0 = 400
            stream = [1 if rng.random() < p0 else 0 for _ in range(t0)] + [1 if rng.random() < p1 else 0 for _ in range(300)]
            d = det(stream)
            if d is None or d < t0:
                miss += d is None
                continue
            delays.append(d - t0)
        fa = sum(1 for _ in range(60) if det([1 if rng.random() < p0 else 0 for _ in range(5000)]) is not None)
        res[name] = {"median_delay_events": statistics.median(delays) if delays else None, "p95_delay_events": sorted(delays)[int(len(delays) * .95) - 1] if delays else None,
                     "missed": miss, "false_alarm_streams_per_60x5000_events": fa}
    return res
