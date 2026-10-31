"""Contract tests: the exact requests we send to Sarvam match docs.sarvam.ai (Oct 2026)."""
import json

import httpx
import pytest

from resolve.config import settings
from resolve.llm.sarvam import Sarvam, SarvamError, split_for_tts, tokens_preserved, tts_normalize


@pytest.fixture
def sv(monkeypatch):
    monkeypatch.setattr(settings, "sarvam_key", "k")
    s = Sarvam()
    s.sent = []

    def handler(req: httpx.Request):
        body = req.content
        s.sent.append((str(req.url), dict(req.headers), body))
        if req.url.path == "/speech-to-text":
            return httpx.Response(200, json={"transcript": "मेरा settlement रुका है", "language_code": "hi-IN", "language_probability": 0.97})
        if req.url.path == "/text-to-speech":
            return httpx.Response(200, json={"audios": ["UklGRg=="]})
        j = json.loads(body)
        return httpx.Response(200, json={"translated_text": s.reply or j["input"]})
    s.reply = None
    s.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return s


async def test_stt_sends_domain_keyterms_for_v4(sv, monkeypatch):
    monkeypatch.setattr(settings, "sarvam_stt_model", "saaras:v4")
    out = await sv.stt(b"audio")
    url, headers, body = sv.sent[0]
    assert url.endswith("/speech-to-text") and headers["api-subscription-key"] == "k"
    assert b'name="keyterms"' in body and "Soundbox".encode() in body and b'name="mode"' not in body  # keyterms are v4-only, mode is v3-only
    assert b"unknown" in body and out["language_probability"] == 0.97


async def test_stt_v3_uses_codemix_mode(sv, monkeypatch):
    monkeypatch.setattr(settings, "sarvam_stt_model", "saaras:v3")
    await sv.stt(b"audio")
    assert b'name="mode"' in sv.sent[0][2] and b"codemix" in sv.sent[0][2] and b"keyterms" not in sv.sent[0][2]


async def test_tts_body(sv, monkeypatch):
    monkeypatch.setattr(settings, "sarvam_tts_model", "bulbul:v3")
    monkeypatch.setattr(settings, "sarvam_tts_speaker", "priya")
    out = await sv.tts("आपका ₹210000 का सेटलमेंट जारी हुआ", "hi")
    body = json.loads(sv.sent[0][2])
    assert body["model"] == "bulbul:v3" and body["language_code"] == "hi-IN" and body["speaker"] == "priya" and 0.5 <= body["pace"] <= 2.0
    assert "2,10,000" in body["text"] and "enable_preprocessing" not in body  # commas for >4 digit numbers; preprocessing is automatic on v3/v4
    assert out["audio_base64"]


async def test_translate_modes_and_models(sv):
    await sv.translate("मेरा settlement S-5001 रुका है", "en", source="hi", purpose="understand")
    b = json.loads(sv.sent[0][2])
    assert b["model"] == "mayura:v1" and b["mode"] == "formal" and b["numerals_format"] == "international" and b["target_language_code"] == "en-IN"
    await sv.translate("Your EMI is due", "hi", source="en", purpose="reply")
    assert json.loads(sv.sent[1][2])["mode"] == "modern-colloquial"
    await sv.translate("Your EMI is due", "ur", source="en", purpose="reply")  # not a Mayura language: Sarvam-Translate, formal only
    b = json.loads(sv.sent[2][2])
    assert b["model"] == "sarvam-translate:v1" and b["mode"] == "formal" and b["target_language_code"] == "ur-IN"


async def test_translation_that_drops_ids_or_amounts_is_refused(sv):
    sv.reply = "Your refund has been started"  # lost ₹1,249 and T-1001
    with pytest.raises(SarvamError):
        await sv.translate("Your refund of ₹1,249 for T-1001 has started", "hi", source="en", purpose="reply")


def test_helpers():
    assert tts_normalize("pay 32000 now, 4999 fine") == "pay 32,000 now, 4999 fine"
    assert tokens_preserved("T-1001 ₹1,249", "टी T-1001 ₹1,249") and not tokens_preserved("T-1001", "no id")
    assert all(len(c) <= 2400 for c in split_for_tts(("Hello world. " * 400)))
