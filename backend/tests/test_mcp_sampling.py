# project/tests/test_mcp_sampling.py
"""Tests d'acceptation — Tache 15 : SamplingPort (reverse LLM, S6 v2.0.0).

Checklist (IMPLEMENTATION_PLAN.md, tache 15) : SamplingPort.create_message
+ SamplingAdapter via LLMClientPort + entities SamplingRequest/Response.
AUCUN reseau — StubLLMClient + fakes locaux. ASCII-only.
"""

from __future__ import annotations

import pytest

from app.domain.entities.mcp import SamplingRequest, SamplingResponse
from app.domain.errors import LLMClientError
from app.domain.ports.mcp_ports import SamplingPort, _sampling_create_text
from app.infrastructure.llm.stub_client import StubLLMClient
from app.infrastructure.mcp.sampling.sampling_adapter import (
    SamplingAdapter,
    build_sampling_adapter,
)


def _messages() -> list[dict]:
    return [
        {"role": "system", "content": "Tu es un assistant."},
        {"role": "user", "content": "Bonjour"},
    ]


def test_sampling_request_defaults() -> None:
    req = SamplingRequest(messages=_messages())
    assert req.max_tokens is None
    assert req.effective_messages() == _messages()


def test_sampling_request_prefixes_system_prompt() -> None:
    req = SamplingRequest(messages=[{"role": "user", "content": "hi"}], system_prompt="sys")
    assert req.effective_messages()[0] == {"role": "system", "content": "sys"}


def test_sampling_request_rejects_empty_messages() -> None:
    with pytest.raises(Exception):
        SamplingRequest(messages=[])  # type: ignore[arg-type]


def test_sampling_request_rejects_bad_role() -> None:
    with pytest.raises(Exception):
        SamplingRequest(messages=[{"role": "", "content": "hi"}])


def test_sampling_request_rejects_empty_content() -> None:
    with pytest.raises(Exception):
        SamplingRequest(messages=[{"role": "user", "content": "  "}])


def test_sampling_request_rejects_bad_max_tokens() -> None:
    with pytest.raises(Exception):
        SamplingRequest(messages=_messages(), max_tokens=0)


def test_sampling_request_is_immutable() -> None:
    req = SamplingRequest(messages=_messages())
    with pytest.raises(Exception):
        req.max_tokens = 10  # type: ignore[misc]


def test_sampling_response_to_dict() -> None:
    resp = SamplingResponse(text="hello", model="m")
    assert resp.to_dict() == {
        "role": "assistant",
        "content": {"type": "text", "text": "hello"},
        "stopReason": "end_turn",
        "model": "m",
    }


def test_sampling_response_to_dict_without_model() -> None:
    assert "model" not in SamplingResponse(text="hello").to_dict()


def test_sampling_response_rejects_empty_text() -> None:
    with pytest.raises(Exception):
        SamplingResponse(text="")


def test_adapter_implements_port() -> None:
    assert isinstance(SamplingAdapter(StubLLMClient()), SamplingPort)


def test_create_message_returns_completion() -> None:
    adapter = SamplingAdapter(StubLLMClient(response="yo"))
    resp = adapter.create_message(SamplingRequest(messages=_messages(), max_tokens=50))
    assert isinstance(resp, SamplingResponse)
    assert resp.text == "yo"


def test_create_message_legacy_list_signature() -> None:
    adapter = SamplingAdapter(StubLLMClient(response="legacy-ok"))
    resp = adapter.create_message(_messages(), max_tokens=32)  # type: ignore[arg-type]
    assert resp.text == "legacy-ok"


def test_create_message_forwards_effective_messages() -> None:
    seen: list = []

    class _Spy:
        def call(self, messages: list[dict]) -> str:
            seen.append(messages)
            return "ok"

    adapter = SamplingAdapter(_Spy())  # type: ignore[arg-type]
    adapter.create_message(
        SamplingRequest(messages=[{"role": "user", "content": "hi"}], system_prompt="S")
    )
    assert seen[0][0] == {"role": "system", "content": "S"}
    assert seen[0][-1] == {"role": "user", "content": "hi"}


def test_create_message_empty_llm_response_raises() -> None:
    adapter = SamplingAdapter(StubLLMClient(response="  "))
    with pytest.raises(LLMClientError):
        adapter.create_message(SamplingRequest(messages=_messages()))


def test_create_message_wraps_provider_error() -> None:
    class _Boom:
        def call(self, messages: list[dict]) -> str:
            raise RuntimeError("down")

    with pytest.raises(LLMClientError):
        SamplingAdapter(_Boom()).create_message(  # type: ignore[arg-type]
            SamplingRequest(messages=_messages())
        )


def test_create_text_delegates_to_create_message() -> None:
    adapter = SamplingAdapter(StubLLMClient(response="txt"))
    assert adapter.create_text(_messages(), system_prompt="S", max_tokens=10) == "txt"


def test_sampling_create_text_helper() -> None:
    adapter = SamplingAdapter(StubLLMClient(response="helper"))
    assert _sampling_create_text(adapter, _messages()) == "helper"


def test_build_sampling_adapter_injects_stub() -> None:
    stub = StubLLMClient(response="built")
    adapter = build_sampling_adapter(stub)
    assert isinstance(adapter, SamplingAdapter)
    assert adapter.llm is stub
    assert adapter.create_text(_messages()) == "built"

