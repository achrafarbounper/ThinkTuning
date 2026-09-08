# project/tests/test_mcp_prompts_5.py
"""Tests d'acceptation - Tache 14 : 5 Prompts MCP (S5, v1.1.0).

Checklist (docs/mcp/IMPLEMENTATION_PLAN.md, tache 14) :
  - summarize-job -> job_id (requis)
  - compare-models -> v1, v2 (requis)
  - explain-prediction -> text (requis)
  - prompt_provider.py -> 5 prompts totaux
  - non-regression tache 9 : les 2 prompts d'origine restent presents.

Le provider est PUR : les tests exercent l'implementation reelle.
Aucune I/O, aucun appel LLM. Fichier ASCII-only (accents evites).
"""

from __future__ import annotations

import json

import pytest

from app.domain.errors import NotFoundError, ValidationError
from app.domain.ports.mcp_ports import MCPPromptRegistryPort
from app.infrastructure.mcp.mcp_server import InMemoryToolProvider, MCPServer
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.prompts.prompt_provider import (
    PROMPT_ANALYZE_SENTIMENT,
    PROMPT_COMPARE_MODELS,
    PROMPT_EXPLAIN_PREDICTION,
    PROMPT_PLAN_TRAINING,
    PROMPT_SUMMARIZE_JOB,
    PromptProvider,
    build_prompt_provider,
)
from app.infrastructure.mcp.protocol import ErrorCode

EXPECTED_PROMPTS = frozenset(
    {
        PROMPT_ANALYZE_SENTIMENT,
        PROMPT_PLAN_TRAINING,
        PROMPT_SUMMARIZE_JOB,
        PROMPT_COMPARE_MODELS,
        PROMPT_EXPLAIN_PREDICTION,
    }
)

EXPECTED_PROMPTS_2 = frozenset({PROMPT_ANALYZE_SENTIMENT, PROMPT_PLAN_TRAINING})

EXPECTED_ARGS = {
    PROMPT_ANALYZE_SENTIMENT: ("text",),
    PROMPT_PLAN_TRAINING: ("dataset",),
    PROMPT_SUMMARIZE_JOB: ("job_id",),
    PROMPT_COMPARE_MODELS: ("v1", "v2"),
    PROMPT_EXPLAIN_PREDICTION: ("text",),
}


def _rpc(server, request_id, method, params=None):
    request = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    return json.loads(server.handle_text(json.dumps(request)))


@pytest.fixture
def provider() -> PromptProvider:
    return build_prompt_provider()


@pytest.fixture
def server(provider: PromptProvider) -> MCPServer:
    return build_mcp_server(tool_provider=InMemoryToolProvider([]))


def test_provider_implements_port(provider):
    assert isinstance(provider, MCPPromptRegistryPort)


def test_prompt_name_constants(provider):
    assert {p.name for p in provider.list_prompts()} == EXPECTED_PROMPTS
    assert PROMPT_SUMMARIZE_JOB == "summarize-job"
    assert PROMPT_COMPARE_MODELS == "compare-models"
    assert PROMPT_EXPLAIN_PREDICTION == "explain-prediction"


def test_list_prompts_returns_five_in_order(provider):
    prompts = provider.list_prompts()
    assert [p.name for p in prompts] == [
        PROMPT_ANALYZE_SENTIMENT,
        PROMPT_PLAN_TRAINING,
        PROMPT_SUMMARIZE_JOB,
        PROMPT_COMPARE_MODELS,
        PROMPT_EXPLAIN_PREDICTION,
    ]


def test_list_prompts_keeps_two_original(provider):
    names = {p.name for p in provider.list_prompts()}
    assert EXPECTED_PROMPTS_2 <= names


def test_list_prompts_arguments(provider):
    by_name = {p.name: p for p in provider.list_prompts()}
    for name, args in EXPECTED_ARGS.items():
        prompt = by_name[name]
        assert prompt.description
        assert tuple(a.name for a in prompt.arguments) == args
        assert all(a.required is True for a in prompt.arguments)
        assert all(a.description for a in prompt.arguments)


def test_list_prompts_projection(provider):
    for prompt in provider.list_prompts():
        proj = prompt.to_dict()
        assert set(proj) == {"name", "description", "arguments"}
        for arg in proj["arguments"]:
            assert set(arg) == {"name", "description", "required"}
            assert arg["required"] is True

def test_get_prompt_all_five(provider):
    from app.infrastructure.mcp.prompts.prompt_provider import _TEMPLATES
    cases = [
        (PROMPT_ANALYZE_SENTIMENT, {"text": "hello"}),
        (PROMPT_PLAN_TRAINING, {"dataset": "data/train.csv"}),
        (PROMPT_SUMMARIZE_JOB, {"job_id": "j-1"}),
        (PROMPT_COMPARE_MODELS, {"v1": "va", "v2": "vb"}),
        (PROMPT_EXPLAIN_PREDICTION, {"text": "hello"}),
    ]
    for name, args in cases:
        (msg,) = provider.get_prompt(name, dict(args))
        assert msg.role == "user"
        assert msg.content == _TEMPLATES[name].format(**args)


def test_get_prompt_compare_models_needs_v2(provider):
    with pytest.raises(ValidationError, match="v2"):
        provider.get_prompt(PROMPT_COMPARE_MODELS, {"v1": "va"})


def test_get_prompt_missing_job_id(provider):
    with pytest.raises(ValidationError, match="job_id"):
        provider.get_prompt(PROMPT_SUMMARIZE_JOB, {})


def test_get_prompt_none_args(provider):
    with pytest.raises(ValidationError, match="text"):
        provider.get_prompt(PROMPT_EXPLAIN_PREDICTION, None)


def test_get_prompt_unknown_lists_five(provider):
    with pytest.raises(NotFoundError, match="Prompt inconnu"):
        provider.get_prompt("nope", {"text": "x"})
    try:
        provider.get_prompt("nope", {})
    except NotFoundError as exc:
        for name in EXPECTED_PROMPTS:
            assert name in str(exc)
    else:
        raise AssertionError("NotFoundError attendu")


def test_get_prompt_non_string(provider):
    with pytest.raises(ValidationError, match="doit"):
        provider.get_prompt(PROMPT_SUMMARIZE_JOB, {"job_id": {"evil": True}})


def test_get_prompt_extra_ignored(provider):
    from app.infrastructure.mcp.prompts.prompt_provider import _TEMPLATES
    args = {"v1": "a", "v2": "b", "extra": "ignored"}
    (msg,) = provider.get_prompt(PROMPT_COMPARE_MODELS, args)
    assert msg.content == _TEMPLATES[PROMPT_COMPARE_MODELS].format(v1="a", v2="b")


def test_get_prompt_no_reformat(provider):
    from app.infrastructure.mcp.prompts.prompt_provider import _TEMPLATES
    (msg,) = provider.get_prompt(PROMPT_SUMMARIZE_JOB, {"job_id": "{job_id}"})
    base = _TEMPLATES[PROMPT_SUMMARIZE_JOB]
    assert msg.content == base.format(job_id="{job_id}")
    assert "{job_id}" in msg.content


def test_get_prompt_projection(provider):
    from app.infrastructure.mcp.prompts.prompt_provider import _TEMPLATES
    (msg,) = provider.get_prompt(PROMPT_EXPLAIN_PREDICTION, {"text": "ok"})
    expected = _TEMPLATES[PROMPT_EXPLAIN_PREDICTION].format(text="ok")
    assert msg.to_dict() == {
        "role": "user",
        "content": {"type": "text", "text": expected},
    }


def test_prompts_list_returns_five(server):
    reply = _rpc(server, 1, "prompts/list")
    prompts = reply["result"]["prompts"]
    assert {p["name"] for p in prompts} == EXPECTED_PROMPTS
    assert EXPECTED_PROMPTS_2 <= {p["name"] for p in prompts}
    for prompt in prompts:
        assert set(prompt) == {"name", "description", "arguments"}


def test_prompts_get_new_templates(server):
    from app.infrastructure.mcp.prompts.prompt_provider import _TEMPLATES
    cases = [
        (2, PROMPT_SUMMARIZE_JOB, {"job_id": "j-1"}),
        (3, PROMPT_COMPARE_MODELS, {"v1": "va", "v2": "vb"}),
        (4, PROMPT_EXPLAIN_PREDICTION, {"text": "hello"}),
    ]
    for rid, name, args in cases:
        reply = _rpc(server, rid, "prompts/get", {"name": name, "arguments": args})
        result = reply["result"]
        assert result["description"]
        assert result["messages"] == [
            {"role": "user",
             "content": {"type": "text", "text": _TEMPLATES[name].format(**args)}}
        ]


def test_prompts_get_missing_arg_invalid_params(server):
    reply = _rpc(server, 5, "prompts/get",
                 {"name": PROMPT_COMPARE_MODELS, "arguments": {"v1": "a"}})
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS
    assert "v2" in reply["error"]["message"]


def test_prompts_get_unknown_invalid_params(server):
    reply = _rpc(server, 6, "prompts/get", {"name": "nope", "arguments": {}})
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS
    assert "Prompt inconnu" in reply["error"]["message"]


def test_factory_wires_five_by_default():
    srv = build_mcp_server(tool_provider=InMemoryToolProvider([]))
    reply = _rpc(srv, 7, "prompts/list")
    assert {p["name"] for p in reply["result"]["prompts"]} == EXPECTED_PROMPTS


def test_initialize_announces_prompts(server):
    reply = _rpc(server, 8, "initialize", {})
    assert reply["result"]["capabilities"]["prompts"] == {"listChanged": False}

