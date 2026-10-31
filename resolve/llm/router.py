"""Task-based model router: the right model for each job, with a fallback chain that always ends at the local stack.

  task                      primary                          why
  ------------------------  -------------------------------  ------------------------------------------------
  intent vote (Indic text)  Sarvam sarvam-105b               Indic-native understanding of Hindi/Hinglish/regional
  intent vote (English)     Claude (fast model)              cheap, accurate
  intent final decision     local ensemble (ML + rules + LLM vote)  never trust one model; disagreement => human
  diagnosis of novel cases  Claude (reasoning model)         strongest general reasoning, enum-constrained
  critic second opinion     a DIFFERENT family than diagnosis (Sarvam if Claude diagnosed, else Claude)
  investigation (agentic)   Claude tool use -> Sarvam tools -> local triage
  customer/voice replies    Sarvam sarvam-105b-conversations (Indic) / Claude fast (English) -> templates
  speech and translation    Sarvam Saaras / Bulbul / Mayura  (see sarvam.py)
Every call is metered (provider, task, latency, fallback) and shown in the console's AI tab.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Awaitable, Callable, Optional

from ..config import settings
from .claude import ClaudeLLM
from .offline import OfflineLLM
from .openai_compat import OpenAICompatLLM
from .sarvam_llm import SarvamLLM

INDIC = {"hi", "hinglish", "bn", "ta", "te", "mr", "gu", "kn", "ml", "pa", "od"}


class ModelRouter:
    def __init__(self, known: list[str], claude: Optional[ClaudeLLM], sarvam: Optional[SarvamLLM], compat: Optional[OpenAICompatLLM] = None) -> None:
        self.claude, self.sarvam, self.compat, self.offline = claude, sarvam, compat, OfflineLLM()
        self.known = known
        self.usage: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(lambda: {"calls": 0, "fallbacks": 0, "ms": 0.0}))
        parts = [n for n, p in (("claude", claude), ("sarvam", sarvam), ("compat", compat)) if p]
        self.name = "router(" + "+".join(parts + ["local-ml"]) + ")"

    # ------------------------------------------------------------ plumbing
    def _meter(self, task: str, provider: str, t0: float, fb: bool = False) -> None:
        u = self.usage[task][provider]
        u["calls"] += 1
        u["ms"] += (time.perf_counter() - t0) * 1000
        u["fallbacks"] += int(fb)

    def _chain(self, task: str, lang: str = "en") -> list[tuple[str, Any]]:
        c, s, k = self.claude, self.sarvam, self.compat
        if task in ("intent", "compose"):  # Indic-native + hackathon partner first; free OpenAI-compatible next; Claude last (scarce credits)
            first = [("sarvam", s), ("compat", k), ("claude", c)]
        else:  # diagnose, investigate, critique, evidence
            first = [("claude", c), ("sarvam", s), ("compat", k)]
        return [(n, p) for n, p in first if p] + [("local", self.offline)]

    def describe(self) -> dict[str, Any]:
        def chain(t: str, l: str = "en") -> list[str]:
            return [n for n, _ in self._chain(t, l)]
        return {
            "providers": {"claude": bool(self.claude), "sarvam": bool(self.sarvam), "compat": bool(self.compat), "local": True},
            "routes": {"intent_vote_indic": chain("intent", "hi"), "intent_vote_english": chain("intent", "en"), "diagnose": chain("diagnose"),
                       "investigate": chain("investigate"), "critique": [n for n, _ in reversed(self._chain("diagnose")) if n != "local"] + ["local"], "compose_indic": chain("compose", "hi"), "compose_english": chain("compose", "en")},
            "usage": {t: {p: {**v, "avg_ms": round(v["ms"] / v["calls"], 1) if v["calls"] else 0} for p, v in d.items()} for t, d in self.usage.items()},
        }

    # ------------------------------------------------------------ tasks
    async def classify_intent(self, masked_text: str, lang: str) -> dict[str, Any]:
        """LLM vote (when available) + local ensemble. The LLM never has the last word."""
        from ..ml.ensemble import combine
        vote: Optional[dict[str, Any]] = None
        for name, prov in self._chain("intent", lang):
            if name == "local":
                break
            t0 = time.perf_counter()
            before = prov.fallbacks
            vote = await prov.classify_intent(masked_text, lang)
            fb = prov.fallbacks > before
            self._meter("intent", name, t0, fb)
            if not fb:
                break
            vote = None
        out = combine(masked_text, llm_vote=vote)
        out["llm_vote"] = vote["intent"] if vote else None
        return out

    async def compose(self, kind: str, lang: str, facts: dict[str, Any]) -> str:
        for name, prov in self._chain("compose", lang):
            t0 = time.perf_counter()
            before = getattr(prov, "fallbacks", 0)
            text = await prov.compose(kind, lang, facts)
            fb = getattr(prov, "fallbacks", 0) > before
            self._meter("compose", name, t0, fb)
            if not fb:
                return text
        return await self.offline.compose(kind, lang, facts)

    async def diagnose(self, brief: dict[str, Any]) -> dict[str, Any]:
        for name, prov in self._chain("diagnose"):
            t0 = time.perf_counter()
            before = getattr(prov, "fallbacks", 0)
            out = await prov.diagnose(brief)
            fb = getattr(prov, "fallbacks", 0) > before
            self._meter("diagnose", name, t0, fb)
            if not fb:
                out["provider"] = name
                return out
        return await self.offline.diagnose(brief)

    async def critique(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Prefer a different model family than the primary diagnoser (independent opinion)."""
        for name, prov in [(n, p) for n, p in reversed(self._chain("diagnose")) if n != "local"]:
            t0 = time.perf_counter()
            before = getattr(prov, "fallbacks", 0)
            out = await prov.critique(plan)
            fb = getattr(prov, "fallbacks", 0) > before
            self._meter("critique", name, t0, fb)
            if not fb:
                return out
        return await self.offline.critique(plan)

    async def draft_evidence_narrative(self, dispute: dict[str, Any], evidence: dict[str, Any]) -> str:
        for name, prov in [(n, p) for n, p in self._chain("evidence") if n != "local"]:
            t0 = time.perf_counter()
            before = getattr(prov, "fallbacks", 0)
            txt = await prov.draft_evidence_narrative(dispute, evidence)
            fb = getattr(prov, "fallbacks", 0) > before
            self._meter("evidence_pack", name, t0, fb)
            if not fb:
                return txt
        return await self.offline.draft_evidence_narrative(dispute, evidence)

    async def investigate(self, brief: dict[str, Any], tools: list[dict[str, Any]], call_tool: Callable[[str, dict], Awaitable[dict]], max_steps: int = 6) -> dict[str, Any]:
        for name, prov in self._chain("investigate"):
            t0 = time.perf_counter()
            before = getattr(prov, "fallbacks", 0)
            out = await prov.investigate(brief, tools, call_tool, max_steps)
            fb = getattr(prov, "fallbacks", 0) > before
            self._meter("investigate", name, t0, fb)
            if not fb:
                return out
        return await self.offline.investigate(brief, tools, call_tool, max_steps)


def get_llm(known_root_causes: list[str]) -> Any:
    claude = ClaudeLLM(known_root_causes) if settings.claude_enabled else None
    sarvam = SarvamLLM(known_root_causes) if settings.sarvam_enabled and settings.llm_mode != "offline" else None
    compat = OpenAICompatLLM(known_root_causes, settings.compat_base_url, settings.compat_api_key, settings.compat_model) if settings.compat_enabled else None
    if claude or sarvam or compat:
        return ModelRouter(known_root_causes, claude, sarvam, compat)
    return OfflineLLM()
