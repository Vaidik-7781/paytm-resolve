"""Exercise the Claude and Sarvam integrations against mocked HTTP: tool-use loops, auth denial, fallbacks, grounding."""
import json

import httpx
import pytest

from resolve.agents.reasoning import KNOWN
from resolve.config import settings
from resolve.engine import Engine
from resolve.llm.claude import ClaudeLLM
from resolve.llm.router import ModelRouter
from resolve.llm.sarvam_llm import SarvamLLM
from resolve.mockworld.world import build_world


def claude_script(party_to_probe):
    """First turn: tool_use (own txns) + tool_use (someone else's txns); second turn: final JSON."""
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if "tools" not in body:  # the quarantined intent parser call
            return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps({"intent": "unknown", "confidence": 0.5, "entities": {}})}]})
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(200, json={"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "t1", "name": "list_recent_txns", "input": {"party_id": "c_zoya", "days": 14}},
                {"type": "tool_use", "id": "t2", "name": "list_recent_txns", "input": {"party_id": party_to_probe, "days": 14}}]})
        results = body["messages"][-1]["content"]
        denied = "ACCESS_DENIED" in results[1]["content"]
        return httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps({
            "hypothesis": "UNKNOWN", "confidence": 0.5, "reasoning": "own txns look healthy" + ("; cross-account read was denied" if denied else ""),
            "recommended_human_actions": ["Verify QR merchant name"], "evidence": ["2 txns"]})}]})
    return handler, state


@pytest.fixture
def claude_on(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_key", "test-key")
    monkeypatch.setattr(settings, "llm_mode", "auto")


async def test_claude_tool_loop_is_authorized_by_gateway(claude_on):
    handler, state = claude_script("c_rohan")  # the model tries to read another customer's data
    claude = ClaudeLLM(KNOWN)
    claude.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    eng = Engine(world=build_world(), pace=0, llm=ModelRouter(KNOWN, claude, None))
    case = await eng.run_scenario("novel_issue", wait=True)
    inv = next(t for t in case.trace if t.kind == "investigation")
    assert inv.data["investigation"]["model"] == "claude" and "cross-account read was denied" in inv.data["investigation"]["reasoning"]
    denied = [t for t in case.trace if t.agent == "Investigator" and t.data.get("redacted")]
    assert denied and denied[0].data["enforced_by"] == "gateway"
    assert case.status == "ESCALATED" and case.tier == 3  # generative reasoning never lifts a case out of human handling
    assert case.brief["ai_investigation"]["recommended_human_actions"] == ["Verify QR merchant name"]
    assert not [a for a in case.actions if a.tool not in ("create_ticket", "notify_party")]


async def test_claude_failure_falls_back_to_offline_triage(claude_on):
    def boom(req):
        return httpx.Response(500, json={"error": "down"})
    claude = ClaudeLLM(KNOWN)
    claude.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(boom))
    eng = Engine(world=build_world(), pace=0, llm=ModelRouter(KNOWN, claude, None))
    case = await eng.run_scenario("novel_issue", wait=True)
    inv = next(t for t in case.trace if t.kind == "investigation").data["investigation"]
    assert inv["model"] == "offline-triage" and case.status == "ESCALATED"


async def test_claude_rewrite_must_keep_grounded_numbers(claude_on):
    def handler(req):
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Great news, we refunded 9999 rupees!"}]})  # drops the real amount/ids
    claude = ClaudeLLM(KNOWN)
    claude.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    text = await claude.compose("payment_refund_initiated", "en", {"amount": "₹1,249", "txn": "T-1001", "refund_id": "RF-1"})
    assert "₹1,249" in text and "RF-1" in text  # hallucinated rewrite rejected, grounded template used
    assert claude.fallbacks == 1


async def test_sarvam_contract_and_tool_calls(monkeypatch):
    monkeypatch.setattr(settings, "sarvam_key", "sk_test")
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["key"] = str(req.url), req.headers.get("api-subscription-key")
        body = json.loads(req.content)
        seen["model"] = body["model"]
        if "tools" in body and len(body["messages"]) == 2:
            return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "list_recent_txns", "arguments": json.dumps({"party_id": "c_zoya"})}}]}}]})
        if "tools" in body:
            return httpx.Response(200, json={"choices": [{"message": {"content": "<think>hmm</think>" + json.dumps({
                "hypothesis": "UNKNOWN", "confidence": 0.4, "reasoning": "looked at txns", "recommended_human_actions": ["Call the customer"], "evidence": []})}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"intent": "settlement_held", "confidence": 0.9, "entities": {}})}}]})

    sv = SarvamLLM(KNOWN)
    sv.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await sv.classify_intent("mera settlement ruka hai", "hinglish")
    assert out["intent"] == "settlement_held" and seen["url"] == "https://api.sarvam.ai/v1/chat/completions" and seen["key"] == "sk_test" and seen["model"] == "sarvam-105b"
    eng = Engine(world=build_world(), pace=0, llm=ModelRouter(KNOWN, None, sv))
    case = await eng.run_scenario("novel_issue", wait=True)
    inv = next(t for t in case.trace if t.kind == "investigation").data["investigation"]
    assert inv["model"] == "sarvam-105b" and inv["steps"] == 1


async def test_router_meters_usage_and_votes(claude_on, monkeypatch):
    monkeypatch.setattr(settings, "sarvam_key", "sk_test")
    def sv_handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"intent": "settlement_held", "confidence": 0.95, "entities": {}})}}]})
    sv = SarvamLLM(KNOWN)
    sv.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(sv_handler))
    router = ModelRouter(KNOWN, None, sv)
    out = await router.classify_intent("मेरा सेटलमेंट रुका हुआ है", "hi")
    assert out["intent"] == "settlement_held" and out["llm_vote"] == "settlement_held" and out["source"] == "agree"
    assert router.describe()["usage"]["intent"]["sarvam"]["calls"] == 1
    assert router.describe()["routes"]["intent_vote_indic"][0] == "sarvam"  # Indic text goes to the Indic-native model first


async def test_sarvam_alone_does_every_job_without_claude(monkeypatch):
    """No Claude key: Sarvam must handle intent, critique, evidence drafting and investigation (the free-tier setup)."""
    monkeypatch.setattr(settings, "sarvam_key", "sk_test")
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        sys_prompt = body["messages"][0]["content"]
        calls.append(sys_prompt[:30])
        if "critic" in sys_prompt:
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"agree": False, "concerns": ["amount not in evidence"]})}}]})
        if "representment" in sys_prompt:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Invoice INV-8841 and delivery proof are attached."}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"intent": "dispute", "confidence": 0.9, "entities": {}})}}]})

    sv = SarvamLLM(KNOWN)
    sv.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    router = ModelRouter(KNOWN, None, sv)
    assert (await router.critique({"actions": []}))["concerns"] == ["amount not in evidence"]
    assert "INV-8841" in await router.draft_evidence_narrative({"id": "D", "reason": "X", "amount": 1}, {})
    d = router.describe()
    assert d["providers"]["claude"] is False and d["usage"]["critique"]["sarvam"]["calls"] == 1 and d["usage"]["evidence_pack"]["sarvam"]["calls"] == 1


async def test_openai_compatible_provider_contract_and_independent_critic(monkeypatch):
    from resolve.llm.openai_compat import OpenAICompatLLM
    monkeypatch.setattr(settings, "sarvam_key", "sk_test")
    seen = {}

    def compat_handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["model"] = str(req.url), req.headers.get("authorization"), json.loads(req.content)["model"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"agree": True, "concerns": []})}}]})

    def sarvam_handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"root_cause": "UNKNOWN", "confidence": 0.3, "reasoning": "x"})}}]})

    comp = OpenAICompatLLM(KNOWN, "https://api.groq.com/openai/v1/", "gsk_test", "llama-3.3-70b-versatile")
    comp.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(compat_handler))
    sv = SarvamLLM(KNOWN)
    sv.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(sarvam_handler))
    router = ModelRouter(KNOWN, None, sv, comp)
    await router.diagnose({"x": 1})                       # primary reasoner = Sarvam
    out = await router.critique({"actions": []})           # critic = a DIFFERENT family (the compat provider)
    assert out["agree"] and seen["url"] == "https://api.groq.com/openai/v1/chat/completions" and seen["auth"] == "Bearer gsk_test" and seen["model"] == "llama-3.3-70b-versatile"
    u = router.describe()["usage"]
    assert u["diagnose"]["sarvam"]["calls"] == 1 and u["critique"]["compat"]["calls"] == 1
