"""Sarvam AI client: speech-to-text (Saaras), text-to-speech (Bulbul), translation (Mayura / Sarvam-Translate).

Contracts verified against docs.sarvam.ai (Oct 29 2026):
  STT  POST /speech-to-text   multipart: file, model (saaras:v4 default), language_code ('unknown' = auto), keyterms (JSON array, <=50, v4 only),
                              mode (saaras:v3 only: transcribe|translate|verbatim|translit|codemix)  -> {transcript, language_code, language_probability}
  TTS  POST /text-to-speech   json: text (<=2500), language_code, model (bulbul:v3 | bulbul:v4-flash), speaker, pace 0.5-2.0, temperature,
                              dict_id (pronunciation dictionary). Preprocessing is automatic on v3/v4; numbers >4 digits need commas.
  MT   POST /translate        json: input (<=1000 chars mayura / 2000 sarvam-translate), source/target_language_code ('auto' on mayura),
                              model, mode (formal|modern-colloquial|classic-colloquial|code-mixed; mayura), output_script, numerals_format
Auth header: api-subscription-key. Every call degrades gracefully without a key (browser voice path / human handoff).
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

import httpx

from ..config import settings

BASE = "https://api.sarvam.ai"
LANG_CODES = {"hi": "hi-IN", "en": "en-IN", "hinglish": "hi-IN", "bn": "bn-IN", "ta": "ta-IN", "te": "te-IN", "mr": "mr-IN", "gu": "gu-IN", "kn": "kn-IN",
              "ml": "ml-IN", "pa": "pa-IN", "od": "od-IN", "as": "as-IN", "ur": "ur-IN", "ne": "ne-IN", "kok": "kok-IN", "mai": "mai-IN", "ks": "ks-IN",
              "doi": "doi-IN", "brx": "brx-IN", "sa": "sa-IN", "sat": "sat-IN", "sd": "sd-IN", "mni": "mni-IN"}
MAYURA = {"hi", "hinglish", "en", "bn", "gu", "kn", "ml", "mr", "od", "pa", "ta", "te"}  # others go to sarvam-translate:v1 (formal)
TTS_LANGS = {"hi", "hinglish", "en", "bn", "gu", "kn", "ml", "mr", "od", "pa", "ta", "te"}  # Bulbul supports 11 (+ hinglish via hi-IN)
KEYTERMS = ["Paytm", "UPI", "Soundbox", "settlement", "chargeback", "refund", "EMI", "KYC", "IFSC", "NACH", "FASTag", "Postpaid", "payout", "UPI PIN",
            "सेटलमेंट", "चार्जबैक", "रिफंड", "ईएमआई", "केवाईसी", "साउंडबॉक्स", "पेटीएम", "यूपीआई", "बैंक खाता", "पेआउट"]
TOKENS = re.compile(r"[A-Za-z]+-\d+|\d[\d,\.]*")


class SarvamError(RuntimeError):
    pass


def _indian_commas(m: re.Match) -> str:
    n = m.group(0)
    head, tail = n[:-3], n[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:]); head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def tts_normalize(text: str) -> str:
    """Bulbul reads numbers >4 digits as a whole number only when they carry commas (docs). Applied to bare digit runs."""
    return re.sub(r"(?<![\d,\.])\d{5,}(?![\d,])", _indian_commas, text)


def tokens_preserved(src: str, out: str) -> bool:
    """A translation must keep every id and amount from the source verbatim (money must never be 'translated')."""
    return all(t in out for t in TOKENS.findall(src))


def split_for_tts(text: str, limit: int = 2400) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for s in re.split(r"(?<=[.!?।])\s+", text):
        if len(cur) + len(s) + 1 > limit and cur:
            out.append(cur); cur = s
        else:
            cur = (cur + " " + s).strip()
    return out + ([cur] if cur else [])


class Sarvam:
    def __init__(self) -> None:
        self.client_factory: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(timeout=40)

    @property
    def enabled(self) -> bool:
        return settings.sarvam_enabled

    def _h(self) -> dict[str, str]:
        return {"api-subscription-key": settings.sarvam_key}

    async def stt(self, audio: bytes, filename: str = "audio.webm", mime: str = "audio/webm", language: str = "unknown") -> dict[str, Any]:
        if not self.enabled:
            raise SarvamError("SARVAM_API_KEY not set")
        data: dict[str, Any] = {"model": settings.sarvam_stt_model, "language_code": LANG_CODES.get(language, language) if language != "unknown" else "unknown"}
        if settings.sarvam_stt_model == "saaras:v4":
            data["keyterms"] = json.dumps(KEYTERMS, ensure_ascii=False)  # domain vocabulary bias (v4 only)
        else:
            data["mode"] = settings.sarvam_stt_mode  # v3 only
        async with self.client_factory() as c:
            r = await c.post(f"{BASE}/speech-to-text", headers=self._h(), files={"file": (filename, audio, mime)}, data=data)
        if r.status_code >= 400:
            raise SarvamError(f"STT {r.status_code}: {r.text[:200]}")
        j = r.json()
        return {"transcript": j.get("transcript", ""), "language_code": j.get("language_code"), "language_probability": j.get("language_probability")}

    async def tts(self, text: str, lang: str = "hi", pace: float = 1.0) -> dict[str, Any]:
        if not self.enabled:
            raise SarvamError("SARVAM_API_KEY not set")
        text = tts_normalize(text)
        audios: list[str] = []
        for chunk in split_for_tts(text):
            body: dict[str, Any] = {"text": chunk, "language_code": LANG_CODES.get(lang, "hi-IN"), "model": settings.sarvam_tts_model, "pace": max(0.5, min(2.0, pace)),
                                    "temperature": 0.5}
            if settings.sarvam_tts_speaker:
                body["speaker"] = settings.sarvam_tts_speaker
            if settings.sarvam_tts_dict_id:
                body["dict_id"] = settings.sarvam_tts_dict_id
            async with self.client_factory() as c:
                r = await c.post(f"{BASE}/text-to-speech", headers=self._h(), json=body)
            if r.status_code >= 400:
                raise SarvamError(f"TTS {r.status_code}: {r.text[:200]}")
            audios += r.json().get("audios") or []
        return {"audio_base64": audios[0] if audios else "", "audios": audios, "mime": "audio/wav", "model": settings.sarvam_tts_model}

    async def translate(self, text: str, target: str, source: str = "auto", purpose: str = "understand") -> str:
        """purpose='understand' -> formal, literal (for classification); purpose='reply' -> modern-colloquial (customer-facing)."""
        if not self.enabled:
            raise SarvamError("SARVAM_API_KEY not set")
        use_mayura = target in MAYURA and (source in MAYURA or source == "auto")
        source_text = text[:1000 if use_mayura else 2000]

        # Protect transaction IDs and amounts from script/number conversion.
        protected = []
        matches = list(re.finditer(r"[A-Za-z]+-\d+", source_text))
        masked_text = source_text
        for index, match in reversed(list(enumerate(matches))):
            placeholder = f"[[PAYMENTTOKEN{chr(65 + index)}]]"
            protected.append((placeholder, match.group(0)))
            masked_text = masked_text[:match.start()] + placeholder + masked_text[match.end():]

        body: dict[str, Any] = {"input": masked_text, "source_language_code": "auto" if (source == "auto" and use_mayura) else LANG_CODES.get(source, "en-IN"),
                                "target_language_code": LANG_CODES.get(target, target), "model": "mayura:v1" if use_mayura else "sarvam-translate:v1", "numerals_format": "international"}
        if source == "auto" and not use_mayura:
            body["source_language_code"] = "en-IN"
        body["mode"] = ("modern-colloquial" if purpose == "reply" else "formal") if use_mayura else "formal"
        if use_mayura and target == "hinglish":
            body["output_script"] = "roman"
        async with self.client_factory() as c:
            r = await c.post(f"{BASE}/translate", headers=self._h(), json=body)
        if r.status_code >= 400:
            raise SarvamError(f"TRANSLATE {r.status_code}: {r.text[:200]}")
        out = r.json().get("translated_text", masked_text)

        # Reject missing or duplicated placeholders before restoring exact values.
        for placeholder, original in protected:
            if out.count(placeholder) != 1:
                raise SarvamError("translation altered a protected id or amount; refusing it")
            out = out.replace(placeholder, original, 1)

        if not tokens_preserved(source_text, out):
            raise SarvamError("translation dropped an id or amount; refusing it")
        return out


sarvam = Sarvam()
