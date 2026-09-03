"""Tests du client HTTP LLM v2 (``HttpLLMClient``) — entièrement hors réseau.

Utilise ``httpx.MockTransport`` pour simuler les flux NDJSON (Ollama) et SSE
(OpenRouter/HF), le retry, et la conformité au port ``LLMClientPort``.

Objectif : prouver que le remplacement du client legacy
(``ia/agent/llm_client.py``) est un ersatz fidèle derrière le même port, sans
aucun appel réseau ni dépendance à un `.env`.
"""

from __future__ import annotations

import inspect

import httpx
import pytest

from app.domain.ports import LLMClientPort
from app.infrastructure.llm.http_client import (
    HttpLLMClient,
    _build_payload,
    _parse_chunk,
)

LLM_METHODS = ("call", "call_stream")


def _client(handler, provider="ollama", retry_attempts=1, retry_base_delay=0.0,
            **kwargs) -> HttpLLMClient:
    """Construit un client avec transport mocké (params de retry par défaut)."""
    return HttpLLMClient(
        url="http://llm.test",
        model="model-x",
        provider=provider,
        transport=httpx.MockTransport(handler),
        retry_attempts=retry_attempts,
        retry_base_delay=retry_base_delay,
        **kwargs,
    )


# --- Conformité au port ------------------------------------------------------


def test_http_client_is_llm_client_port():
    assert issubclass(HttpLLMClient, LLMClientPort)


def test_http_client_implements_full_port_signature():
    for name in LLM_METHODS:
        port_params = set(inspect.signature(getattr(LLMClientPort, name)).parameters)
        impl_params = set(inspect.signature(getattr(HttpLLMClient, name)).parameters)
        assert port_params <= impl_params, (
            f"HttpLLMClient.{name} : {port_params - impl_params}"
        )


def test_http_client_rejects_unknown_provider():
    with pytest.raises(ValueError):
        HttpLLMClient(url="http://x", model="m", provider="bogus", transport=None)


# --- Payload par provider ----------------------------------------------------


def test_build_payload_ollama_num_ctx():
    p = _build_payload("ollama", "m", [{"role": "user", "content": "hi"}],
                       temperature=0.8, context_length=2048, think=True)
    assert p["stream"] is True
    assert p["options"]["num_ctx"] == 2048
    assert p["think"] is True


def test_build_payload_openrouter_no_options_and_no_think():
    p = _build_payload("openrouter", "m", [], temperature=0.8,
                       context_length=2048, think=True)
    assert "options" not in p
    assert "think" not in p
    assert p["temperature"] == 0.8


def test_build_payload_hf_no_options():
    p = _build_payload("hf", "m", [], temperature=0.8, context_length=2048,
                       think=False)
    assert "options" not in p


# --- Parsing -----------------------------------------------------------------


def test_parse_chunk_handles_ndjson_and_sse():
    assert _parse_chunk(b'{"a":1}') == {"a": 1}
    assert _parse_chunk("data: {\"a\":1}") == {"a": 1}
    assert _parse_chunk("data: [DONE]") is None
    assert _parse_chunk("") is None
    assert _parse_chunk("pas-json") is None


# --- Streaming NDJSON (Ollama) -----------------------------------------------


def test_call_stream_ollama_assembles_and_thinking():
    body = (
        '{"message":{"role":"assistant","content":"Bon"},"done":false}\n'
        '{"message":{"thinking":"réflexion"},"done":false}\n'
        '{"message":{"role":"assistant","content":"jour"},"done":true}\n'
    ).encode()

    def handler(request):
        return httpx.Response(200, content=body,
                              headers={"content-type": "application/x-ndjson"})

    client = _client(handler, provider="ollama")
    content_events: list[str] = []
    thinking_events: list[str] = []
    out = client.call_stream(
        [{"role": "user", "content": "salut"}],
        on_content=content_events.append,
        on_thinking=thinking_events.append,
    )
    assert out == "Bonjour"
    assert client.last_thinking == "réflexion"
    assert "".join(content_events) == "Bonjour"
    assert "".join(thinking_events) == "réflexion"


def test_call_ollama_returns_str_contract():
    body = ('{"message":{"content":"réponse finale"},"done":true}\n').encode()

    def handler(request):
        return httpx.Response(200, content=body,
                              headers={"content-type": "application/x-ndjson"})

    client = _client(handler, provider="ollama")
    assert client.call([{"role": "user", "content": "q"}]) == "réponse finale"


# --- Streaming SSE (OpenRouter / HF) -----------------------------------------


def test_call_stream_openrouter_sse_reasoning():
    body = (
        "data: {\"choices\":[{\"delta\":{\"content\":\"Bon\"}}]}\n"
        "data: {\"choices\":[{\"delta\":{\"reasoning\":\"réfl\"}}]}\n"
        "data: {\"choices\":[{\"delta\":{\"content\":\"jour\"}}]}\n"
        "data: [DONE]\n"
    ).encode()

    def handler(request):
        return httpx.Response(200, content=body,
                              headers={"content-type": "text/event-stream"})

    client = _client(handler, provider="openrouter")
    thinking_events: list[str] = []
    out = client.call_stream(
        [{"role": "user", "content": "q"}],
        on_thinking=thinking_events.append,
    )
    assert out == "Bonjour"
    assert "".join(thinking_events) == "réfl"
    assert client.last_thinking == "réfl"


# --- Fiabilité : retry sur erreur éphémère ----------------------------------


def test_retry_recovers_after_5xx():
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, content=b"")
        return httpx.Response(200, content=(
            b'{"message":{"content":"ok"},"done":true}\n'
        ))

    # retry_attempts=2 : la 1re tentative (503) est re-tentée.
    client = _client(handler, provider="ollama", retry_attempts=2)
    assert client.call([{"role": "user", "content": "q"}]) == "ok"
    assert len(calls) == 2
    assert client.last_error is None


def test_non_retryable_http_error_raises_immediately():
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, content=b"")  # 4xx = définitif

    client = _client(handler, provider="ollama", retry_attempts=3)
    with pytest.raises(httpx.HTTPStatusError):
        client.call([{"role": "user", "content": "q"}])
    assert len(calls) == 1  # aucun retry sur erreur définitive
