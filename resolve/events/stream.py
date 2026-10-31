"""Live event hub (SSE fan-out) and the transaction-stream incident detector (proactive mode)."""
from __future__ import annotations

import asyncio
import random
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any, Optional

from ..mockworld.world import World
from ..models import Incident, Party, Txn, uid, utcnow


class Hub:
    def __init__(self) -> None:
        self.subs: set[asyncio.Queue] = set()
        self.history: deque = deque(maxlen=600)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)

    def publish(self, type_: str, payload: dict[str, Any]) -> None:
        msg = {"type": type_, "ts": utcnow().isoformat(), "data": payload}
        self.history.append(msg)
        for q in list(self.subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                self.subs.discard(q)


class IncidentDetector:
    """Sliding-window failure-rate detector per issuer bank.

    Opens an incident when failures >= min_failures AND failure rate >= rate_threshold inside the window.
    (Production: replace with a streaming z-score / CUSUM over Kafka; the contract stays the same.)
    """

    def __init__(self, world: World, window_min: int = 10, min_failures: int = 12, rate_threshold: float = 0.35) -> None:
        self.world = world
        self.window = timedelta(minutes=window_min)
        self.min_failures = min_failures
        self.rate_threshold = rate_threshold
        self.win: dict[str, deque] = defaultdict(deque)  # bank -> deque[(ts, failed, txn_id)]
        self.cusum: dict[str, float] = defaultdict(float)  # one-sided CUSUM on failure indicator (baseline ~4%): earlier detection, same false-alarm rate
        self.p0, self.k, self.h = 0.04, 0.12, 4.0

    def ingest(self, txn: Txn) -> Optional[Incident]:
        bank = txn.bank
        failed = txn.status == "FAILED" or (txn.debit == "DEBITED" and txn.credit != "CREDITED")
        dq = self.win[bank]
        dq.append((txn.created_at, failed, txn.id))
        cutoff = utcnow() - self.window
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        fails = [t for (_, f, t) in dq if f]
        rate = len(fails) / max(1, len(dq))
        open_for_bank = next((i for i in self.world.incidents.values() if i.cluster == bank and i.status == "OPEN"), None)
        if open_for_bank:
            open_for_bank.failure_count = len(fails)
            open_for_bank.failure_rate = round(rate, 3)
            new = [t for t in fails if t not in open_for_bank.affected_txns]
            open_for_bank.affected_txns += new
            return None
        self.cusum[bank] = max(0.0, self.cusum[bank] + (1.0 if failed else 0.0) - self.p0 - self.k)
        cusum_trip = self.cusum[bank] > self.h and len(fails) >= 5
        if (len(fails) >= self.min_failures and rate >= self.rate_threshold) or cusum_trip:
            inc = Incident(id=self.world.next_id("INC"), cluster=bank, opened_at=utcnow(), failure_count=len(fails),
                           failure_rate=round(rate, 3), affected_txns=list(fails),
                           suspected_cause=f"Issuer-side degradation at {bank}: {len(fails)} failures in {int(self.window.total_seconds() // 60)} min "
                                           f"({rate:.0%} failure rate vs a typical low single-digit baseline)")
            self.world.incidents[inc.id] = inc
            self.world.bank_state[bank] = "DEGRADED"
            self.world.log("incidents", "opened", incident=inc.id, bank=bank, failures=len(fails))
            return inc
        return None

    def resolve(self, incident_id: str) -> None:
        inc = self.world.incidents.get(incident_id)
        if inc:
            inc.status = "RESOLVED"
            self.world.bank_state[inc.cluster] = "OK"
            self.win[inc.cluster].clear()
            self.cusum[inc.cluster] = 0.0


def synth_customers(world: World, n: int = 30) -> list[str]:
    ids = []
    langs = ["en", "hi", "hinglish", "hinglish", "en"]
    for i in range(n):
        pid = f"c_syn_{i:03d}"
        if pid not in world.parties:
            world.parties[pid] = Party(id=pid, kind="customer", name=f"Customer {i:03d}", language=langs[i % len(langs)])
        ids.append(pid)
    return ids


def simulate_outage_txns(world: World, bank: str, failures: int = 40, healthy: int = 14, seed: int = 7) -> list[Txn]:
    """Create a burst of failing debits on `bank` mixed with healthy traffic, as the stream would deliver."""
    rng = random.Random(seed)
    payers = synth_customers(world)
    flags = [True] * failures + [False] * healthy
    rng.shuffle(flags)
    out: list[Txn] = []
    now = utcnow()
    for failed in flags:
        t_bank = bank if failed or rng.random() < 0.25 else rng.choice(["Alpha Bank", "Gamma Bank"])
        failed = failed and t_bank == bank
        tid = world.next_id("T")
        txn = Txn(id=tid, payer_id=rng.choice(payers), merchant_id=rng.choice(["m_sharma", "m_chai", "m_urban"]),
                  amount=float(rng.choice([199, 349, 499, 799, 1250, 2100])), bank=t_bank, handle="@" + t_bank.split()[0].lower(),
                  debit="DEBITED", credit="FAILED" if failed else "CREDITED", status="FAILED" if failed else "SUCCESS",
                  created_at=now - timedelta(seconds=rng.randint(5, 540)))
        world.txns[tid] = txn
        out.append(txn)
    return out
