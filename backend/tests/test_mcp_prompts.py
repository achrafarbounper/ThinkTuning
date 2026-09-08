# project/tests/test_mcp_prompts.py
"""Tests d'acceptation — Tâche 9 : 2 Prompts MCP (S3, v1.0.0 Beta).

Checklist (docs/mcp/IMPLEMENTATION_PLAN.md, tâche 9) :

    - ``PromptProvider`` expose les 2 prompts via ``list_prompts()`` →
      ``prompts/list`` ;
    - entité « MCPPrompt » : ``MCPPromptTemplate`` (``name``, ``description``,
      ``arguments``) + ``MCPPromptArgument`` (name/description/required) —
      posées à la tâche 3, réutilisées telles quelles ;
    - ``get_prompt(name, arguments)`` résout le template → messages :
        - ``analyze-sentiment`` → « Analyse le sentiment de ce texte: {text} » ;
        - ``plan-training`` → « Planifie un entraînement pour: {dataset} » ;
    - sécurité : prompt inconnu → ``NotFoundError``, argument requis manquant
      → ``ValidationError``, valeur non-string → ``ValidationError``,
      arguments surnuméraires ignorés, valeurs substituées non re-traitées ;
    - serveur : ``prompts/list`` retourne les 2, ``prompts/get`` résout
      (``{description?, messages: [{role, content: {type: text, text}}]}``),
      traduit ``NotFoundError``/``ValidationError`` en ``Invalid params``
      (-32602, jamais un crash) et annonce la capability ``prompts`` à
      l'``initialize``.

Le provider est PUR (catalogue statique, aucune I/O) : les tests unitaires
exercent l'implémentation réelle — aucun fake nécessaire.
"""

from __future__ import annotations

import json

import pytest

from app.domain.entities.mcp import MCPScopeRole, MCPVersion
from app.domain.errors import NotFoundError, ValidationError
from app.domain.ports.mcp_ports import MCPPromptRegistryPort
from app.infrastructure.mcp.mcp_server import InMemoryToolProvider, MCPServer
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.prompts.prompt_provider import (
    PROMPT_ANALYZE_SENTIMENT,
    PROMPT_PLAN_TRAINING,
    PromptProvider,
    build_prompt_provider,
)
from app.infrastructure.mcp.protocol import ErrorCode


def _rpc(
    server: MCPServer,
    request_id: int,
    method: str,
    params: dict | None = None,
) -> dict:
    """Aller-retour JSON-RPC : requête → réponse parsée (id préservé)."""
    request: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    return json.loads(server.handle_text(json.dumps(request)))


@pytest.fixture
def provider() -> PromptProvider:
    return build_prompt_provider()


@pytest.fixture
def server(provider: PromptProvider) -> MCPServer:
    """Serveur MCP minimal : aucun tool, resources par défaut, prompts réels."""
    return build_mcp_server(tool_provider=InMemoryToolProvider([]))


# ============================================================
# Port — contrat
# ============================================================


def test_provider_implements_mcp_prompt_registry_port(provider: PromptProvider) -> None:
    """PromptProvider (infra) implémente MCPPromptRegistryPort — fail-fast."""
    assert isinstance(provider, MCPPromptRegistryPort)


# ============================================================
# ListPrompts — les 2 prompts (métadonnées)
# ============================================================


def test_list_prompts_returns_exactly_the_two_prompts(provider: PromptProvider) -> None:
    """``list_prompts()`` -> les 2 prompts tache 9 en tete, ordre deterministe.

    Tache 14 (S5, v1.1.0) : la surface est etendue a 5 - ces 2 prompts
    d'origine restent un PREFIXE garanti (non-regression), les 3 nouveaux
    etant couverts exhaustivement par tests/test_mcp_prompts_5.py.
    """
    prompts = provider.list_prompts()
    assert [prompt.name for prompt in prompts][:2] == [
        PROMPT_ANALYZE_SENTIMENT,
        PROMPT_PLAN_TRAINING,
    ]


def test_list_prompts_metadata_shape(provider: PromptProvider) -> None:
    """Chaque prompt porte name/description + SON argument (requis)."""
    by_name = {prompt.name: prompt for prompt in provider.list_prompts()}
    sentiment = by_name[PROMPT_ANALYZE_SENTIMENT]
    assert sentiment.description
    assert len(sentiment.arguments) == 1
    (text_arg,) = sentiment.arguments
    assert text_arg.name == "text"
    assert text_arg.required is True
    assert text_arg.description

    training = by_name[PROMPT_PLAN_TRAINING]
    assert training.description
    assert len(training.arguments) == 1
    (dataset_arg,) = training.arguments
    assert dataset_arg.name == "dataset"
    assert dataset_arg.required is True


def test_list_prompts_projection_matches_mcp_spec(provider: PromptProvider) -> None:
    """``to_dict()`` projette ``{name, description, arguments}`` (prompts/list)."""
    for prompt in provider.list_prompts():
        projection = prompt.to_dict()
        assert set(projection) == {"name", "description", "arguments"}
        for argument in projection["arguments"]:
            assert set(argument) == {"name", "description", "required"}
            assert argument["required"] is True


# ============================================================
# GetPrompt — résolution des templates
# ============================================================


def test_get_prompt_analyze_sentiment(provider: PromptProvider) -> None:
    """``analyze-sentiment`` → template résolu avec la valeur de ``text``."""
    messages = provider.get_prompt(
        PROMPT_ANALYZE_SENTIMENT, {"text": "Ce film est génial !"}
    )
    assert len(messages) == 1
    assert messages[0].role == "user"
    assert messages[0].content == (
        "Analyse le sentiment de ce texte: Ce film est génial !"
    )


def test_get_prompt_plan_training(provider: PromptProvider) -> None:
    """``plan-training`` → template résolu avec la valeur de ``dataset``."""
    messages = provider.get_prompt(PROMPT_PLAN_TRAINING, {"dataset": "data/train.csv"})
    assert len(messages) == 1
    assert messages[0].role == "user"
    assert messages[0].content == "Planifie un entraînement pour: data/train.csv"


def test_get_prompt_message_projection(provider: PromptProvider) -> None:
    """Le message résolu projette ``{role, content: {type: text, text}}`` (MCP)."""
    (message,) = provider.get_prompt(PROMPT_ANALYZE_SENTIMENT, {"text": "ok"})
    assert message.to_dict() == {
        "role": "user",
        "content": {"type": "text", "text": "Analyse le sentiment de ce texte: ok"},
    }


def test_get_prompt_extra_arguments_are_ignored(provider: PromptProvider) -> None:
    """Surplus d'arguments : ignoré (pas d'erreur différentielle, spec MCP)."""
    messages = provider.get_prompt(
        PROMPT_ANALYZE_SENTIMENT, {"text": "hello", "extra": "ignored"}
    )
    assert messages[0].content == "Analyse le sentiment de ce texte: hello"


def test_get_prompt_substituted_values_are_not_reformatted(
    provider: PromptProvider,
) -> None:
    """Une valeur contenant ``{...}`` n'est PAS re-traitée comme un gabarit."""
    messages = provider.get_prompt(PROMPT_ANALYZE_SENTIMENT, {"text": "{text} requis"})
    assert messages[0].content == "Analyse le sentiment de ce texte: {text} requis"


# ============================================================
# GetPrompt — erreurs (fail-closed)
# ============================================================


def test_get_prompt_unknown_name_is_not_found(provider: PromptProvider) -> None:
    """Nom inconnu → NotFoundError, message actionnable (catalogue public)."""
    with pytest.raises(NotFoundError, match="Prompt inconnu"):
        provider.get_prompt("nope", {"text": "x"})


def test_get_prompt_missing_required_argument_is_validation_error(
    provider: PromptProvider,
) -> None:
    """Argument requis absent → ValidationError (client-réparable)."""
    with pytest.raises(ValidationError, match="text"):
        provider.get_prompt(PROMPT_ANALYZE_SENTIMENT, {})


def test_get_prompt_none_arguments_is_validation_error(
    provider: PromptProvider,
) -> None:
    """``arguments=None`` → ValidationError (l'argument requis manque)."""
    with pytest.raises(ValidationError, match="dataset"):
        provider.get_prompt(PROMPT_PLAN_TRAINING, None)


def test_get_prompt_non_string_value_is_validation_error(
    provider: PromptProvider,
) -> None:
    """Valeur non-string → ValidationError : on n'interpole JAMAIS un objet
    arbitraire (la spec MCP ne transporte que des chaînes)."""
    with pytest.raises(ValidationError, match="chaîne"):
        provider.get_prompt(PROMPT_ANALYZE_SENTIMENT, {"text": {"evil": True}})


# ============================================================
# Serveur MCP — prompts/list & prompts/get (JSON-RPC)
# ============================================================


def test_prompts_list_returns_the_two_prompts(server: MCPServer) -> None:
    """``prompts/list`` -> les 2 prompts tache 9 inclus (surface etendue a 5).

    Non-regression tache 9 : les 2 noms d'origine restent presents ; la
    couverture exhaustive des 5 vit dans tests/test_mcp_prompts_5.py.
    """
    reply = _rpc(server, 1, "prompts/list")
    prompts = reply["result"]["prompts"]
    assert {
        PROMPT_ANALYZE_SENTIMENT,
        PROMPT_PLAN_TRAINING,
    } <= {p["name"] for p in prompts}
    for prompt in prompts:
        assert set(prompt) == {"name", "description", "arguments"}


def test_prompts_get_resolves_template(server: MCPServer) -> None:
    """``prompts/get`` → description + messages résolus (format MCP)."""
    reply = _rpc(
        server,
        2,
        "prompts/get",
        {"name": PROMPT_ANALYZE_SENTIMENT, "arguments": {"text": "Super produit"}},
    )
    result = reply["result"]
    assert result["description"]
    assert result["messages"] == [
        {
            "role": "user",
            "content": {
                "type": "text",
                "text": "Analyse le sentiment de ce texte: Super produit",
            },
        }
    ]


def test_prompts_get_unknown_name_is_invalid_params(server: MCPServer) -> None:
    """Prompt inconnu → erreur ``Invalid params`` (jamais un crash)."""
    reply = _rpc(server, 3, "prompts/get", {"name": "nope", "arguments": {}})
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS
    assert "Prompt inconnu" in reply["error"]["message"]


def test_prompts_get_missing_required_argument_is_invalid_params(
    server: MCPServer,
) -> None:
    """Argument requis manquant → ``Invalid params`` (ValidationError traduite)."""
    reply = _rpc(
        server, 4, "prompts/get", {"name": PROMPT_PLAN_TRAINING, "arguments": {}}
    )
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS
    assert "dataset" in reply["error"]["message"]


def test_prompts_get_without_name_is_invalid_params(server: MCPServer) -> None:
    """``prompts/get`` sans ``name`` → Invalid params."""
    reply = _rpc(server, 5, "prompts/get", {})
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS
    assert "'name'" in reply["error"]["message"]


def test_prompts_get_with_non_object_arguments_is_invalid_params(
    server: MCPServer,
) -> None:
    """``arguments`` non-objet → Invalid params (validation de forme)."""
    reply = _rpc(
        server, 6, "prompts/get", {"name": PROMPT_ANALYZE_SENTIMENT, "arguments": "text"}
    )
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS


def test_prompts_surface_without_registry_is_indistinguishable() -> None:
    """Sans registre : ``prompts/list`` vide + ``prompts/get`` → Invalid params.

    Comportement v0.1.0 des constructions sur mesure : une surface sans
    prompts est indiscernable d'un prompt inconnu (aucun oracle).
    """
    bare = MCPServer(
        version=MCPVersion(major=0, minor=1, patch=0),
        scope=MCPScopeRole.READ_ONLY,
        tool_provider=InMemoryToolProvider([]),
        resource_provider=None,
        prompt_provider=None,
    )
    listing = _rpc(bare, 7, "prompts/list")
    assert listing["result"]["prompts"] == []
    reply = _rpc(bare, 8, "prompts/get", {"name": PROMPT_ANALYZE_SENTIMENT})
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS
    assert "no prompt registry" in reply["error"]["message"]


def test_initialize_announces_prompts_capability(server: MCPServer) -> None:
    """``initialize`` annonce la capability ``prompts`` (registre branché)."""
    reply = _rpc(server, 9, "initialize", {})
    assert reply["result"]["capabilities"]["prompts"] == {"listChanged": False}
    # …et un serveur construit SANS registre ne l'annonce pas.
    bare = MCPServer(
        version=MCPVersion(major=0, minor=1, patch=0),
        scope=MCPScopeRole.READ_ONLY,
        tool_provider=InMemoryToolProvider([]),
        prompt_provider=None,
    )
    bare_caps = _rpc(bare, 10, "initialize", {})["result"]["capabilities"]
    assert "prompts" not in bare_caps
