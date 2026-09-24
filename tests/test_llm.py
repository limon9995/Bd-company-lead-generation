import json

import httpx
import respx

from app.services.llm import GeminiProvider, parse_json_loose


def test_parse_json_loose():
    assert parse_json_loose('```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert parse_json_loose('Here you go: {"ok": true} thanks') == {"ok": True}


@respx.mock
def test_gemini_provider_uses_real_sdk_request_shape():
    route = respx.post(url__regex=r"https://generativelanguage\.googleapis\.com/.*/models/gemini-flash-lite-latest:generateContent")
    route.mock(return_value=httpx.Response(200, json={
        "candidates": [{"content": {"role": "model", "parts": [{"text": '[{"name": "Rahim Uddin"}]'}]}, "finishReason": "STOP"}]}))
    llm = GeminiProvider("test-key", "gemini-flash-lite-latest")
    assert llm.generate_json("extract", "system") == [{"name": "Rahim Uddin"}]
    req = route.calls[0].request
    body = json.loads(req.content)
    assert req.headers.get("x-goog-api-key") == "test-key"
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["systemInstruction"]["parts"][0]["text"] == "system"
