"""FastAPI app: REST + Server-Sent Events + static console."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import ROOT, settings
from ..engine import Engine
from ..events.stream import Hub
from ..evals.harness import run_all
from ..evals.scenarios import SCENARIOS
from ..evals.synth import run_synthetic
from ..llm.sarvam import SarvamError, sarvam
from ..mockworld.world import build_world

WEB = ROOT / "web"
app = FastAPI(title="Paytm Resolve", version="1.0")
HUB = Hub()


async def offload(fn, *args):
    """Run a heavy evaluation in its own thread + event loop so SSE streaming and the live demo never stall."""
    return await asyncio.to_thread(lambda: asyncio.run(fn(*args)))


class State:
    engine: Engine
    evals: Optional[dict[str, Any]] = None
    synth: Optional[dict[str, Any]] = None


S = State()
S.engine = Engine(world=build_world(), hub=HUB)


def E() -> Engine:
    return S.engine


def case_json(case_id: str) -> dict[str, Any]:
    c = E().cases.get(case_id)
    if not c:
        raise HTTPException(404, "case not found")
    d = c.model_dump(mode="json")
    d["pending"] = c.status in ("WORKING", "OPEN")
    return d


# ------------------------------------------------------------------------------------------------ models
class NewCase(BaseModel):
    party_id: str
    message: str
    channel: str = "chat"


class Reply(BaseModel):
    text: str


class Human(BaseModel):
    by: str = "ops.lead"
    note: str = ""


class UndoReq(BaseModel):
    action_id: str
    by: str = "ops.lead"


class Outage(BaseModel):
    bank: str = "Beta Bank"
    failures: int = 40


class Autonomy(BaseModel):
    category: str
    mode: str


class SynthReq(BaseModel):
    n: int = 200
    seed: int = 11
    noise: float = 0.0


class TTSReq(BaseModel):
    text: str
    lang: str = "hi"


class TranslateReq(BaseModel):
    text: str
    target: str


# ------------------------------------------------------------------------------------------------ state / config
@app.get("/api/config")
def config() -> dict[str, Any]:
    return {"llm": E().llm.name, "claude": settings.claude_enabled, "sarvam": settings.sarvam_enabled, "compat": settings.compat_enabled, "compat_model": settings.compat_model, "pace": E().pace, "policy_version": E().policy.version,
            "models": {"main": settings.anthropic_model, "fast": settings.anthropic_fast_model}}


@app.get("/api/state")
def state() -> dict[str, Any]:
    e = E()
    cases = sorted(e.cases.values(), key=lambda c: c.created_at, reverse=True)
    return {
        "config": config(), "metrics": e.metrics(), "cases": [e.case_summary(c) for c in cases[:80]],
        "incidents": [i.model_dump(mode="json") for i in e.world.incidents.values()], "autonomy": e.policy.book.summary(),
        "scenarios": [{"key": k, "title": v["title"], "group": v.get("group", ""), "hero": bool(v.get("hero")), "party": v["party"], "message": v["message"],
                       "has_followup": bool(v.get("followups"))} for k, v in SCENARIOS.items()],
        "parties": [p.model_dump(mode="json") for p in e.world.parties.values() if not p.id.startswith("c_syn_")][:30],
        "evals": S.evals, "synth": S.synth,
    }


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    q = HUB.subscribe()

    async def gen():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=12)
                    yield f"event: {msg['type']}\ndata: {json.dumps(msg['data'], default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            HUB.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------------------------------------ cases
@app.post("/api/cases")
async def new_case(body: NewCase) -> dict[str, Any]:
    if not E().world.get_party(body.party_id):
        raise HTTPException(404, "unknown party")
    c = await E().submit(body.party_id, body.message, channel=body.channel)
    return {"id": c.id}


@app.get("/api/cases")
def list_cases() -> list[dict[str, Any]]:
    e = E()
    return [e.case_summary(c) for c in sorted(e.cases.values(), key=lambda c: c.created_at, reverse=True)[:100]]


@app.get("/api/cases/{case_id}")
def get_case(case_id: str) -> dict[str, Any]:
    return case_json(case_id)


@app.post("/api/cases/{case_id}/reply")
async def reply(case_id: str, body: Reply) -> dict[str, Any]:
    if case_id not in E().cases:
        raise HTTPException(404, "case not found")
    asyncio.create_task(E().reply(case_id, body.text))
    return {"ok": True}


def _guard(fn):
    async def run(case_id: str, body: Human):
        try:
            await fn(case_id, body)
        except (ValueError, KeyError) as ex:
            raise HTTPException(400, str(ex))
        return case_json(case_id)
    return run


@app.post("/api/cases/{case_id}/approve")
async def approve(case_id: str, body: Human) -> dict[str, Any]:
    try:
        await E().approve(case_id, body.by, body.note)
    except (ValueError, KeyError) as ex:
        raise HTTPException(400, str(ex))
    return case_json(case_id)


@app.post("/api/cases/{case_id}/reject")
async def reject(case_id: str, body: Human) -> dict[str, Any]:
    try:
        await E().reject(case_id, body.by, body.note)
    except (ValueError, KeyError) as ex:
        raise HTTPException(400, str(ex))
    return case_json(case_id)


@app.post("/api/cases/{case_id}/resolve")
async def resolve(case_id: str, body: Human) -> dict[str, Any]:
    try:
        await E().human_resolve(case_id, body.by, body.note)
    except KeyError as ex:
        raise HTTPException(400, str(ex))
    return case_json(case_id)


@app.post("/api/cases/{case_id}/undo")
async def undo(case_id: str, body: UndoReq) -> dict[str, Any]:
    try:
        await E().undo(case_id, body.action_id, body.by)
    except (ValueError, KeyError) as ex:
        raise HTTPException(400, str(ex))
    return case_json(case_id)


# ------------------------------------------------------------------------------------------------ scenarios / simulation
@app.post("/api/scenarios/{key}/run")
async def run_scenario(key: str) -> dict[str, Any]:
    if key not in SCENARIOS:
        raise HTTPException(404, "unknown scenario")
    c = await E().run_scenario(key)
    return {"id": c.id, "followups": [f[0] + (":" + f[1] if f[1] else "") for f in SCENARIOS[key].get("followups", [])]}


@app.post("/api/sim/outage")
async def outage(body: Outage) -> dict[str, Any]:
    return await E().handle_outage(body.bank, body.failures)


@app.post("/api/sim/outage/resolve/{incident_id}")
def outage_resolve(incident_id: str) -> dict[str, Any]:
    E().resolve_incident(incident_id)
    return {"ok": True}


@app.post("/api/proactive/scan")
async def proactive_scan() -> dict[str, Any]:
    ids = await E().proactive_scan()
    return {"cases": ids}


@app.post("/api/world/reset")
def reset_world() -> dict[str, Any]:
    S.engine = Engine(world=build_world(), hub=HUB)
    HUB.publish("reset", {})
    return {"ok": True}


@app.get("/api/world")
def world() -> dict[str, Any]:
    return E().world.snapshot()


# ------------------------------------------------------------------------------------------------ evals
@app.post("/api/evals/run")
async def evals_run() -> dict[str, Any]:
    S.evals = await offload(run_all)
    return S.evals


@app.post("/api/evals/synthetic")
async def evals_synth(body: SynthReq) -> dict[str, Any]:
    S.synth = await offload(run_synthetic, min(max(body.n, 10), 1000), body.seed, min(max(body.noise, 0.0), 0.8))
    return S.synth


# ------------------------------------------------------------------------------------------------ policy / gateway / knowledge
@app.get("/api/policy")
def policy() -> dict[str, Any]:
    e = E()
    return {"policy": e.policy.policy, "autonomy": e.policy.book.summary()}


@app.post("/api/policy/autonomy")
def set_autonomy(body: Autonomy) -> dict[str, Any]:
    try:
        E().policy.book.set_mode(body.category, body.mode)
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    HUB.publish("autonomy", {"autonomy": E().policy.book.summary()})
    return {"autonomy": E().policy.book.summary()}


@app.get("/api/gateway/tools")
def tools() -> list[dict[str, Any]]:
    return E().gateway.catalog()


@app.get("/api/gateway/calls")
def calls(limit: int = 80) -> list[dict[str, Any]]:
    return E().gateway.calls[-limit:][::-1]


@app.get("/api/knowledge/search")
def kb_search(q: str, k: int = 4) -> list[dict[str, Any]]:
    return E().kb.search(q, k)


# ------------------------------------------------------------------------------------------------ voice (Sarvam) and translation
@app.post("/api/voice/stt")
async def stt(file: UploadFile = File(...), language: str = "unknown") -> JSONResponse:
    try:
        out = await sarvam.stt(await file.read(), file.filename or "audio.webm", file.content_type or "audio/webm", language)
        return JSONResponse(out)
    except SarvamError as ex:
        return JSONResponse({"error": str(ex), "fallback": "browser"}, status_code=501)


@app.post("/api/voice/tts")
async def tts(body: TTSReq) -> JSONResponse:
    try:
        return JSONResponse(await sarvam.tts(body.text, body.lang))
    except SarvamError as ex:
        return JSONResponse({"error": str(ex), "fallback": "browser"}, status_code=501)


@app.post("/api/translate")
async def translate(body: TranslateReq) -> JSONResponse:
    try:
        return JSONResponse({"text": await sarvam.translate(body.text, body.target)})
    except SarvamError as ex:
        return JSONResponse({"error": str(ex)}, status_code=501)


LANG_MATRIX = [
    {"code": "en", "name": "English", "nlu": "local ML + rules (+ LLM vote)", "replies": "native templates", "stt_tts": "Sarvam / browser"},
    {"code": "hi", "name": "Hindi (Devanagari)", "nlu": "local ML + rules (+ Sarvam vote)", "replies": "native templates + Sarvam conversational rewrite", "stt_tts": "Sarvam / browser"},
    {"code": "hinglish", "name": "Hinglish (code-mixed)", "nlu": "local ML + rules (+ Sarvam vote)", "replies": "native templates + Sarvam conversational rewrite", "stt_tts": "Sarvam / browser"},
] + [{"code": c, "name": n, "nlu": "Sarvam Mayura translate → local stack", "replies": "Sarvam Mayura translate from English", "stt_tts": "Sarvam"}
     for c, n in [("bn", "Bengali"), ("ta", "Tamil"), ("te", "Telugu"), ("mr", "Marathi"), ("gu", "Gujarati"), ("kn", "Kannada"), ("ml", "Malayalam"), ("pa", "Punjabi"), ("od", "Odia")]]


class IntentTry(BaseModel):
    text: str


@app.get("/api/ai/overview")
def ai_overview() -> dict[str, Any]:
    from ..ml.intent_model import get_model
    e = E()
    router = e.llm.describe() if hasattr(e.llm, "describe") else {"providers": {"claude": False, "sarvam": False, "local": True}, "routes": {}, "usage": {}}
    return {"llm": e.llm.name, "router": router, "languages": LANG_MATRIX, "intent_model": {**get_model().meta, "labels": get_model().labels, "temperature": get_model().T},
            "sarvam": {"enabled": settings.sarvam_enabled, "models": {"chat_reasoning": "sarvam-105b", "chat_voice": "sarvam-105b-conversations", "stt": settings.sarvam_stt_model,
                                                                    "tts": settings.sarvam_tts_model, "translate": settings.sarvam_translate_model}},
            "claude": {"enabled": settings.claude_enabled, "models": {"reasoning": settings.anthropic_model, "fast": settings.anthropic_fast_model}}}


@app.post("/api/ai/intent")
async def ai_intent(body: IntentTry) -> dict[str, Any]:
    from ..guard import inspect
    from ..llm.i18n import detect_language
    g = inspect(body.text)
    lang = detect_language(body.text)
    res = await E().llm.classify_intent(g.masked_text or g.clean_text or body.text, lang)
    return {"language": lang, "guard": {"flags": g.flags, "removed": g.removed, "pii": g.pii_found, "masked": g.masked_text}, "result": res}


_INTENT_EVAL: dict[str, Any] = {}


@app.post("/api/ai/intent-eval")
def ai_intent_eval() -> dict[str, Any]:
    from ..ml.eval import run
    _INTENT_EVAL["r"] = run(200)
    return _INTENT_EVAL["r"]


@app.get("/api/ai/intent-eval")
def ai_intent_eval_get() -> dict[str, Any]:
    return _INTENT_EVAL.get("r", {})


# ------------------------------------------------------------------------------------------------ rigor: conformal, selective, bench, feedback, audit
class Label(BaseModel):
    case_id: str
    intent: str


@app.post("/api/ai/rigor")
def ai_rigor() -> dict[str, Any]:
    from ..ml import conformal
    return {"conformal": conformal.evaluate(0.1), "selective": conformal.selective()}


@app.post("/api/ai/benchmark")
async def ai_benchmark() -> dict[str, Any]:
    from ..evals.bench import cost_model, detector_eval, latency_and_throughput
    perf = await offload(latency_and_throughput, 300, 50)
    return {"perf": perf, "cost": cost_model(), "detector": await asyncio.to_thread(detector_eval)}


@app.get("/api/feedback")
def feedback_state() -> dict[str, Any]:
    from ..ml import feedback
    from ..ml.intent_model import get_model
    return {"buffer": feedback.BUFFER[-20:], "count": len(feedback.BUFFER), "history": feedback.HISTORY, "model": get_model().meta}


@app.post("/api/feedback/label")
def feedback_label(body: Label) -> dict[str, Any]:
    from ..ml import feedback
    c = E().cases.get(body.case_id)
    ctx = E().ctxs.get(body.case_id)
    if not c or not ctx:
        raise HTTPException(404, "case not found")
    text = ctx.facts.get("text_for_model") or c.message
    try:
        return feedback.add(text, body.intent, c.id)
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@app.post("/api/feedback/retrain")
def feedback_retrain() -> dict[str, Any]:
    from ..ml import feedback
    try:
        return feedback.retrain()
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@app.post("/api/feedback/rollback")
def feedback_rollback() -> dict[str, Any]:
    from ..ml import feedback
    try:
        return feedback.rollback()
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@app.get("/api/cases/{case_id}/audit")
def case_audit(case_id: str, fmt: str = "json") -> Any:
    from .. import audit
    c = E().cases.get(case_id)
    if not c:
        raise HTTPException(404, "case not found")
    p = audit.pack(c, E())
    if fmt == "md":
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(audit.to_markdown(p), media_type="text/markdown")
    return p


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True}


# ------------------------------------------------------------------------------------------------ pages
@app.get("/customer")
def customer_page() -> FileResponse:
    return FileResponse(WEB / "customer.html")


app.mount("/", StaticFiles(directory=str(WEB), html=True), name="web")
