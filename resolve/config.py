"""Runtime configuration (environment driven)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


@dataclass
class Settings:
    # Seconds of pause between trace steps so the console animates live. 0 in tests/evals.
    pace: float = field(default_factory=lambda: float(os.getenv("RESOLVE_PACE", "0.45")))
    llm_mode: str = field(default_factory=lambda: os.getenv("LLM_MODE", "auto"))  # auto | offline | claude
    anthropic_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    anthropic_model: str = field(default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"))
    anthropic_fast_model: str = field(default_factory=lambda: os.getenv("ANTHROPIC_FAST_MODEL", "claude-haiku-5-5"))
    sarvam_key: str = field(default_factory=lambda: os.getenv("SARVAM_API_KEY", ""))
    sarvam_tts_model: str = field(default_factory=lambda: os.getenv("SARVAM_TTS_MODEL", "bulbul:v3"))  # or bulbul:v4-flash (fastest)
    sarvam_tts_speaker: str = field(default_factory=lambda: os.getenv("SARVAM_TTS_SPEAKER", "priya"))  # v3 short name; for v4-flash use a persona id or leave empty
    sarvam_tts_dict_id: str = field(default_factory=lambda: os.getenv("SARVAM_TTS_DICT_ID", ""))  # pronunciation dictionary for Paytm/UPI/KYC
    sarvam_stt_mode: str = field(default_factory=lambda: os.getenv("SARVAM_STT_MODE", "codemix"))  # only used with saaras:v3
    sarvam_stt_model: str = field(default_factory=lambda: os.getenv("SARVAM_STT_MODEL", "saaras:v4"))
    sarvam_translate_model: str = field(default_factory=lambda: os.getenv("SARVAM_TRANSLATE_MODEL", "mayura:v1"))
    # Any OpenAI-compatible chat endpoint: Groq, Google Gemini (OpenAI-compat), OpenRouter, Ollama, LM Studio, vLLM...
    compat_base_url: str = field(default_factory=lambda: os.getenv("OPENAI_COMPAT_BASE_URL", ""))
    compat_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_COMPAT_API_KEY", ""))
    compat_model: str = field(default_factory=lambda: os.getenv("OPENAI_COMPAT_MODEL", ""))
    gate_secret: str = field(default_factory=lambda: os.getenv("RESOLVE_GATE_SECRET", "dev-only-secret-change-me"))
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8000")))

    @property
    def claude_enabled(self) -> bool:
        return self.llm_mode != "offline" and bool(self.anthropic_key)

    @property
    def compat_enabled(self) -> bool:
        return self.llm_mode != "offline" and bool(self.compat_base_url and self.compat_model)

    @property
    def sarvam_enabled(self) -> bool:
        return bool(self.sarvam_key)


settings = Settings()
