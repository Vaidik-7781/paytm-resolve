"""Generic OpenAI-compatible provider: Groq, Google Gemini (OpenAI-compat endpoint), OpenRouter, Ollama, LM Studio, vLLM.

Configure in .env: OPENAI_COMPAT_BASE_URL, OPENAI_COMPAT_MODEL, OPENAI_COMPAT_API_KEY (optional for local servers).
It reuses the Sarvam chat provider logic (quarantined intent parsing, grounded rewrites, critic, bounded tool-calling investigator),
so every safety property (enum-only outputs, read-only tools, grounding checks, offline fallback) is identical.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from .sarvam_llm import SarvamLLM


class OpenAICompatLLM(SarvamLLM):
    def __init__(self, known_root_causes: list[str], base_url: str, api_key: str, model: str) -> None:
        super().__init__(known_root_causes)
        self.base_url, self.api_key, self.model = base_url.rstrip("/"), api_key, model
        self.model_label = model
        self.name = f"compat:{model}"
        self.client_factory = lambda: httpx.AsyncClient(timeout=60)

    async def _chat(self, model: str, messages: list[dict[str, Any]], max_tokens: int = 500, temperature: float = 0.2, tools: Optional[list[dict]] = None) -> dict[str, Any]:
        self.calls += 1
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens, "reasoning_effort": None}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with self.client_factory() as c:
            r = await c.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
        r.raise_for_status()
        return r.json()["choices"][0]
