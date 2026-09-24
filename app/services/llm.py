"""LLM provider interface. Gemini is the default; add another class with the same methods to switch."""
import json
import re
from typing import Protocol

from app.services.errors import ProviderError


class LLMProvider(Protocol):
    def generate_json(self, prompt: str, system: str = "") -> object: ...

    def generate_text(self, prompt: str, system: str = "", max_tokens: int = 400) -> str: ...


def parse_json_loose(text: str) -> object:
    text = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        m = re.search(r"(\[.*\]|\{.*\})", text, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        raise ProviderError(f"LLM did not return valid JSON: {text[:200]}") from exc


class GeminiProvider:
    def __init__(self, api_key: str, model: str):
        from google import genai  # imported lazily so the web app starts even if the SDK misbehaves

        self._genai = genai
        self.client = genai.Client(api_key=api_key)
        self.model = model

    def _call(self, prompt: str, system: str, **cfg) -> str:
        from google.genai import types

        try:
            resp = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(system_instruction=system or None, temperature=0.2, **cfg),
            )
        except Exception as exc:  # SDK raises its own error hierarchy
            raise ProviderError(f"Gemini error: {exc}") from exc
        return resp.text or ""

    def generate_json(self, prompt: str, system: str = "") -> object:
        return parse_json_loose(self._call(prompt, system, response_mime_type="application/json"))

    def generate_text(self, prompt: str, system: str = "", max_tokens: int = 400) -> str:
        # No max_output_tokens: on "thinking" models it also caps hidden reasoning tokens and can
        # return empty text. Length is controlled in the prompt instead.
        return self._call(prompt, system).strip()


def list_gemini_models(api_key: str) -> list[str]:
    from google import genai

    client = genai.Client(api_key=api_key)
    names = []
    for m in client.models.list():
        actions = getattr(m, "supported_actions", None) or []
        if not actions or "generateContent" in actions:
            names.append((m.name or "").removeprefix("models/"))
    return sorted(n for n in names if "gemini" in n)
