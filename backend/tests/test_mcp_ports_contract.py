"""Tests de contrat des ports MCP (S1 — Tâche 3).

Vérifie que les implémentations existantes (infrastructure/legacy) implémentent
correctement les ports du domaine MCP, et que les nouvelles entités du domaine
projettent correctement vers le format MCP.

Scope S1 (tâche 3 — docs/mcp/IMPLEMENTATION_PLAN.md) :
    - MCPToolRegistryPort : InMemoryToolProvider (infrastructure) le implémente
      (structural typing via ``@runtime_checkable``) ;
    - MCPResourceRegistryPort / MCPPromptRegistryPort / SamplingPort : pas
      d'implémentation concrète en infra à la S1 — des fakes vérifient le
      contrat pour garantir la compatibilité future.

Le socle MCP est self-contained (AUCUNE dépendance au SDK ``mcp``) : ces tests
n'importent rien de lourd (ni torch, ni transformers) — le paquet ``app`` est
suffisant.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, dataclass
from typing import Any

import pytest

from app.domain.entities.mcp import (
    MCPResource,
    MCPResourceTemplate,
    MCPPromptArgument,
    MCPPromptMessage,
    MCPPromptTemplate,
    MCPScopeRole,
    MCPTool,
    MCPVersion,
)
from app.domain.ports.mcp_ports import (
    MCPResourceRegistryPort,
    MCPPromptRegistryPort,
    MCPToolRegistryPort,
    SamplingPort,
)
from app.domain.ports.ports import Message
from app.infrastructure.mcp.mcp_server import (
    InMemoryToolProvider,
    ToolError,
)
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.protocol import empty_input_schema


# ============================================================
# MCPToolRegistryPort — InMemoryToolProvider (legacy) le implémente
# ============================================================


def test_in_memory_tool_provider_implements_port() -> None:
    """InMemoryToolProvider (infra) implémente MCPToolRegistryPort — fail-fast."""
    provider = InMemoryToolProvider([])
    assert isinstance(provider, MCPToolRegistryPort)


def test_tool_provider_list_tools_returns_mcp_tool() -> None:
    """list_tools rend des MCPTool du domaine (même type, même projection)."""
    tool = MCPTool(
        name="test_tool",
        description="Un tool de test",
        input_schema=empty_input_schema(),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
        required_scope=MCPScopeRole.READ_ONLY,
        handler=lambda _: "ok",
    )
    provider = InMemoryToolProvider([tool])
    assert isinstance(provider, MCPToolRegistryPort)

    tools = provider.list_tools()
    assert len(tools) == 1
    assert isinstance(tools[0], MCPTool)
    assert tools[0].name == "test_tool"


def test_tool_provider_call_tool_executes_handler() -> None:
    """call_tool exécute le handler et rend le texte (port contract)."""

    def _echo(args: dict[str, Any]) -> str:
        return f"echo: {args.get('msg', '')}"

    tool = MCPTool(
        name="echo",
        description="Renvoie le message",
        input_schema=empty_input_schema(),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
        required_scope=MCPScopeRole.READ_ONLY,
        handler=_echo,
    )
    provider = InMemoryToolProvider([tool])
    assert isinstance(provider, MCPToolRegistryPort)
    assert provider.call_tool("echo", {"msg": "hello"}) == "echo: hello"


def test_tool_provider_call_unknown_raises_tool_error() -> None:
    """call_tool sur un nom inconnu lève ToolError (erreur métier → isError)."""
    provider = InMemoryToolProvider([])
    with pytest.raises(ToolError, match="Unknown tool"):
        provider.call_tool("nope", {})


def test_mcp_server_accepts_mcp_tool_registry_port() -> None:
    """MCPServer accepte un MCPToolRegistryPort — wiring découplé du provider."""
    tool = MCPTool(
        name="greeting",
        description="Salue l'utilisateur",
        input_schema=empty_input_schema(),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
        required_scope=MCPScopeRole.READ_ONLY,
        handler=lambda _: "hello world",
    )
    assert isinstance(InMemoryToolProvider([tool]), MCPToolRegistryPort)

    server = build_mcp_server(tool_provider=InMemoryToolProvider([tool]))
    reply = json.loads(server.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })))
    names = {t["name"] for t in reply["result"]["tools"]}
    assert "greeting" in names

    call_reply = json.loads(server.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "greeting", "arguments": {}},
    })))
    assert call_reply["result"]["isError"] is False
    assert call_reply["result"]["content"][0]["text"] == "hello world"


# ============================================================
# MCPTool entity — immutabilité + projection
# ============================================================


def test_mcp_tool_is_immutable() -> None:
    """MCPTool est immuable (frozen=True) — source de vérité du domaine."""
    tool = MCPTool(
        name="t",
        description="d",
        input_schema=empty_input_schema(),
        annotations={},
        required_scope=MCPScopeRole.READ_ONLY,
        handler=lambda _: "ok",
    )
    with pytest.raises((AttributeError, FrozenInstanceError)):
        tool.name = "changed"  # type: ignore[misc]


def test_mcp_tool_to_dict_keys_match_mcp() -> None:
    """to_dict projette exactement les clés MCP (name, description, inputSchema, annotations)."""
    tool = MCPTool(
        name="my_tool",
        description="A tool",
        input_schema={"type": "object", "properties": {}},
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
        required_scope=MCPScopeRole.READ_ONLY,
        handler=lambda _: "ok",
    )
    d = tool.to_dict()
    assert set(d.keys()) == {"name", "description", "inputSchema", "annotations"}
    assert d["name"] == "my_tool"
    assert d["inputSchema"]["type"] == "object"
    assert d["annotations"]["readOnlyHint"] is True


# ============================================================
# MCPResourceTemplate + MCPPromptArgument entities
# ============================================================


def test_resource_template_to_dict_projection() -> None:
    """MCPResourceTemplate.to_dict projette le format MCP resources/templates."""
    arg = MCPPromptArgument(name="job_id", description="ID du job", required=True)
    template = MCPResourceTemplate(
        uri_template="thinktuning://job/{job_id}",
        name="Job",
        description="Détails d'un job d'entraînement",
        mime_type="application/json",
        arguments=(arg,),
    )
    d = template.to_dict()
    assert d["uriTemplate"] == "thinktuning://job/{job_id}"
    assert d["mimeType"] == "application/json"
    assert d["arguments"][0]["name"] == "job_id"
    assert d["arguments"][0]["required"] is True
    assert d["arguments"][0]["description"] == "ID du job"


def test_resource_template_defaults() -> None:
    """Les champs optionnels sont omis si vides — format MCP minimal."""
    template = MCPResourceTemplate(
        uri_template="thinktuning://health",
        name="Health",
    )
    d = template.to_dict()
    assert set(d.keys()) == {"uriTemplate", "name", "mimeType"}
    assert d["mimeType"] == "text/plain"  # valeur par défaut, non omise


def test_prompt_argument_to_dict() -> None:
    """MCPPromptArgument.to_dict projette {name, description?, required}."""
    arg = MCPPromptArgument(name="text", description="Texte à analyser", required=True)
    d = arg.to_dict()
    assert d == {"name": "text", "description": "Texte à analyser", "required": True}

    arg_opt = MCPPromptArgument(name="lang")
    d_opt = arg_opt.to_dict()
    assert d_opt == {"name": "lang", "required": False}


# ============================================================
# MCPPromptRegistryPort — fake contract
# ============================================================


@dataclass(frozen=True)
class _FakePromptRegistry:
    """Fake d'MCPPromptRegistryPort pour le contrat S1 (port non encore en infra)."""

    _prompts: tuple[MCPPromptTemplate, ...] = (
        MCPPromptTemplate(
            name="summarize-job",
            description="Resumé un job d'entraînement",
            arguments=(
                MCPPromptArgument(name="job_id", description="ID du job", required=True),
            ),
        ),
    )

    def list_prompts(self) -> list[MCPPromptTemplate]:
        return list(self._prompts)

    def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> list[MCPPromptMessage]:
        from app.domain.errors import NotFoundError, ValidationError

        if name != "summarize-job":
            raise NotFoundError(f"Prompt inconnu : {name}")
        required = [a.name for a in self._prompts[0].arguments if a.required]
        if not arguments or not all(r in arguments for r in required):
            raise ValidationError("Arguments requis manquants")
        return [
            MCPPromptMessage(
                role="user",
                content=f"Fais un résumé du job {arguments['job_id']} "
                        "statut, durée, métriques.",
            ),
        ]


def test_fake_prompt_registry_implements_port() -> None:
    """Un fake d'MCPPromptRegistryPort passe le isinstance + comportement."""
    fake = _FakePromptRegistry()
    assert isinstance(fake, MCPPromptRegistryPort)

    prompts = fake.list_prompts()
    assert len(prompts) == 1
    assert prompts[0].name == "summarize-job"
    assert isinstance(prompts[0], MCPPromptTemplate)

    d = prompts[0].to_dict()
    assert set(d.keys()) == {"name", "description", "arguments"}
    assert d["arguments"][0]["required"] is True

    msgs = fake.get_prompt("summarize-job", {"job_id": "job-42"})
    assert len(msgs) == 1
    assert msgs[0].role == "user"
    assert "job-42" in msgs[0].content

    msg_dict = msgs[0].to_dict()
    assert msg_dict["role"] == "user"
    assert msg_dict["content"]["type"] == "text"
    assert "job-42" in msg_dict["content"]["text"]


def test_fake_prompt_registry_missing_required_arg() -> None:
    """get_prompt lève ValidationError si argument requis absent."""
    from app.domain.errors import ValidationError

    fake = _FakePromptRegistry()
    with pytest.raises(ValidationError):
        fake.get_prompt("summarize-job", {})


def test_fake_prompt_registry_unknown_prompt() -> None:
    """get_prompt lève NotFoundError si le prompt est inconnu."""
    from app.domain.errors import NotFoundError

    fake = _FakePromptRegistry()
    with pytest.raises(NotFoundError):
        fake.get_prompt("unknown", {"job_id": "x"})


# ============================================================
# MCPResourceRegistryPort — fake contract
# ============================================================


@dataclass(frozen=True)
class _FakeResourceRegistry:
    """Fake d'MCPResourceRegistryPort — contrat S3 (tâche 8 : resources listées)."""

    def list_resources(self) -> list[MCPResource]:
        return [
            MCPResource(
                uri="thinktuning://job/{job_id}",
                name="Job",
                description="Détails d'un job d'entraînement",
                mime_type="application/json",
            ),
        ]

    def read_resource(self, uri: str) -> str:
        from app.domain.errors import NotFoundError

        if uri.startswith("thinktuning://job/"):
            return json.dumps({"job_id": uri.rsplit("/", 1)[-1], "status": "running"})
        raise NotFoundError(f"Resource not found : {uri}")


def test_fake_resource_registry_implements_port() -> None:
    """Un fake d'MCPResourceRegistryPort passe le isinstance + comportement."""
    fake = _FakeResourceRegistry()
    assert isinstance(fake, MCPResourceRegistryPort)

    resources = fake.list_resources()
    assert len(resources) == 1
    assert isinstance(resources[0], MCPResource)
    assert resources[0].uri == "thinktuning://job/{job_id}"

    content = fake.read_resource("thinktuning://job/abc-123")
    data = json.loads(content)
    assert data["job_id"] == "abc-123"
    assert data["status"] == "running"


def test_fake_resource_registry_unknown_uri() -> None:
    """read_resource lève NotFoundError pour une URI inconnue."""
    from app.domain.errors import NotFoundError

    fake = _FakeResourceRegistry()
    with pytest.raises(NotFoundError):
        fake.read_resource("thinktuning://unknown/xyz")


# ============================================================
# SamplingPort — fake contract
# ============================================================


@dataclass(frozen=True)
class _FakeSamplingPort:
    """Fake de SamplingPort pour le contrat S1 (reverse LLM inference)."""

    def create_text(
        self,
        messages: list[Message],
        *,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        last = messages[-1].get("content", "") if messages else ""
        if isinstance(last, list):
            last = " ".join(c.get("text", "") for c in last if isinstance(c, dict))
        return f"[sampled] {last}"


def test_fake_sampling_port_implements_port() -> None:
    """Un fake de SamplingPort passe le isinstance + rend du texte."""
    fake = _FakeSamplingPort()
    assert isinstance(fake, SamplingPort)

    messages: list[Message] = [{"role": "user", "content": "Bonjour"}]
    result = fake.create_text(
        messages,
        system_prompt="Tu es un assistant.",
        max_tokens=50,
        temperature=0.7,
    )
    assert result == "[sampled] Bonjour"


def test_fake_sampling_port_empty_messages() -> None:
    """create_text gère une liste de messages vide sans crash."""
    fake = _FakeSamplingPort()
    result = fake.create_text([])
    assert result == "[sampled] "


# ============================================================
# MCPServer bootstrap — port + scope fail-closed
# ============================================================


def test_server_uses_domain_mcp_tool() -> None:
    """Le bootstrap du serveur utilise MCPTool du domaine (to_dict projection)."""
    server = build_mcp_server(version=MCPVersion.parse("0.1.0"))
    reply = json.loads(server.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })))
    tools = reply["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"mcp_version", "server_info"} <= names
    for t in tools:
        assert set(t.keys()) == {"name", "description", "inputSchema", "annotations"}


def test_scope_filters_tools_by_port() -> None:
    """Le scope (grâce au port) masque les tools requiretant un rôle supérieur."""
    admin_tool = MCPTool(
        name="admin_only",
        description="Tool sensible",
        input_schema=empty_input_schema(),
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
        required_scope=MCPScopeRole.ADMIN,
        handler=lambda _: "secret",
    )
    read_only_tool = MCPTool(
        name="read_only_tool",
        description="Tool lecture",
        input_schema=empty_input_schema(),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
        required_scope=MCPScopeRole.READ_ONLY,
        handler=lambda _: "ok",
    )
    provider = InMemoryToolProvider([admin_tool, read_only_tool])
    assert isinstance(provider, MCPToolRegistryPort)

    # read_only server : seulement le tool READ_ONLY visible
    ro_server = build_mcp_server(scope=MCPScopeRole.READ_ONLY, tool_provider=provider)
    reply = json.loads(ro_server.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })))
    visible = {t["name"] for t in reply["result"]["tools"]}
    assert "read_only_tool" in visible
    assert "admin_only" not in visible

    # admin server : les deux visibles
    admin_server = build_mcp_server(scope=MCPScopeRole.ADMIN, tool_provider=provider)
    reply2 = json.loads(admin_server.handle_text(json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    })))
    visible2 = {t["name"] for t in reply2["result"]["tools"]}
    assert {"admin_only", "read_only_tool"} <= visible2


# ============================================================
# Entités — immutabilité
# ============================================================


def test_resource_template_is_immutable() -> None:
    """MCPResourceTemplate est immuable (domaine pur)."""
    template = MCPResourceTemplate(
        uri_template="thinktuning://job/{job_id}",
        name="Job",
    )
    with pytest.raises((AttributeError, FrozenInstanceError)):
        template.name = "changed"  # type: ignore[misc]


def test_prompt_template_is_immutable() -> None:
    """MCPPromptTemplate est immuable (domaine pur)."""
    prompt = MCPPromptTemplate(name="test", description="desc")
    with pytest.raises((AttributeError, FrozenInstanceError)):
        prompt.name = "changed"  # type: ignore[misc]


def test_prompt_message_is_immutable() -> None:
    """MCPPromptMessage est immuable (domaine pur)."""
    msg = MCPPromptMessage(role="user", content="hello")
    with pytest.raises((AttributeError, FrozenInstanceError)):
        msg.content = "changed"  # type: ignore[misc]

