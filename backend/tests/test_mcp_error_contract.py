"""Tests du contrat d'erreurs structuré MCP (v2.3.0).

Critères d'acceptation couverts :
    - erreurs de VALIDATION : ``errorType=validation_error``, ``retryable=false``,
      ``fieldErrors`` par champ, aucune fuite de chemin local ;
    - erreurs de POLICY : ``errorType=policy_error``, ``retryable=false`` ;
    - erreurs de TIMEOUT : ``errorType=timeout_error``, ``retryable=true`` ;
    - erreurs INTERNES : ``errorType=internal_error``, message générique ;
    - contrat transversal : ``correlationId`` unique par requête,
      ``retryAfterSeconds`` uniquement quand un délai est connu,
      réponses sans erreur inchangées (compatibilité 2.2.x).
"""

from __future__ import annotations

import json

import pytest

from app.domain.entities.mcp import MCPScopeRole, MCPTool, MCPVersion
from app.domain.errors import (
    BudgetExceededError,
    GatewayTimeoutError,
    LLMClientError,
    NotFoundError,
    PolicyUnavailableError,
    SandboxViolationError,
    ValidationError,
)
from app.infrastructure.mcp.error_contract import (
    MCPErrorType,
    MCPStructuredError,
    fallback_from_rpc_code,
    new_correlation_id,
    sanitize_message,
    structured_from_domain_error,
    structured_from_enforcer_error,
    structured_rate_limited,
)
from app.infrastructure.mcp.mcp_server import InMemoryToolProvider, MCPServer
from app.infrastructure.mcp.protocol import ErrorCode, error_result
from app.infrastructure.mcp.security.scope_enforcer import (
    MCPAccessDeniedError,
    MCPQuotaExceededError,
    MCPRateLimitExceededError,
)


class _StubSamplingPort:
    """Port de sampling minimal (lève une ValidationError métier sur appel)."""

    def create_message(self, request: object) -> object:
        raise LLMClientError("provider down")


def _server() -> MCPServer:
    """Serveur MCP minimal (2 tools purs, aucun transport)."""

    def _ok(args: dict[str, object]) -> str:
        return "ok"

    tools = [
        MCPTool(
            name="echo",
            description="echo",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": True},
            required_scope=MCPScopeRole.READ_ONLY,
            handler=_ok,
        ),
    ]
    return MCPServer(
        version=MCPVersion.parse("2.3.0"),
        scope=MCPScopeRole.ADMIN,
        tool_provider=InMemoryToolProvider(tools),
        sampling_port=_StubSamplingPort(),  # type: ignore[arg-type]
    )


def _call(server: MCPServer, raw: str) -> dict[str, object]:
    response = server.handle_text(raw)
    assert response is not None
    return json.loads(response)


def _error_data(response: dict[str, object]) -> dict[str, object]:
    error = response["error"]
    assert isinstance(error, dict)
    assert "data" in error, "toute erreur MCP 2.3.0 porte le contrat structuré"
    data = error["data"]
    assert isinstance(data, dict)
    assert set(data) >= {"errorType", "retryable"}
    return data


# ---------------------------------------------------------------------------
# 1. Validation — fieldErrors par champ, non retryable, pas de fuite de chemin
# ---------------------------------------------------------------------------


def test_sampling_validation_error_is_structured_per_field() -> None:
    server = _server()
    response = _call(
        server, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "sampling/create", "params": {}})
    )
    data = _error_data(response)
    assert data["errorType"] == MCPErrorType.VALIDATION.value
    assert data["retryable"] is False
    assert "messages" in data.get("fieldErrors", {})


def test_validation_message_never_leaks_local_path() -> None:
    sanitized = sanitize_message(
        "fichier introuvable : D:\\workspace\\ThinkTuning\\backend\\app\\secret.py"
    )
    assert "D:" not in sanitized
    assert "[redacted-path]" in sanitized


def test_sanitizer_redacts_api_keys() -> None:
    sanitized = sanitize_message("appel refusé api_key=sk-abc123 et Authorization: Bearer xyz")
    assert "sk-abc123" not in sanitized
    assert "xyz" not in sanitized
    assert "[redacted]" in sanitized


# ---------------------------------------------------------------------------
# 2. Policy — non retryable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (PolicyUnavailableError("PDP indisponible"), "PDP indisponible"),
        (SandboxViolationError("commande interdite"), "commande interdite"),
        (BudgetExceededError("budget épuisé"), "budget épuisé"),
    ],
)
def test_policy_domain_errors_map_to_policy_type(exc: Exception, message: str) -> None:
    contract = structured_from_domain_error(exc)  # type: ignore[arg-type]
    assert contract.error_type is MCPErrorType.POLICY
    assert contract.effective_retryable is False
    assert contract.message == message


def test_enforcer_access_denied_is_policy() -> None:
    contract = structured_from_enforcer_error(MCPAccessDeniedError("accès refusé"))
    assert contract.error_type is MCPErrorType.POLICY
    assert contract.effective_retryable is False


def test_enforcer_quota_exceeded_is_policy() -> None:
    contract = structured_from_enforcer_error(MCPQuotaExceededError("quota épuisé"))
    assert contract.error_type is MCPErrorType.POLICY


def test_enforcer_rate_limit_carries_retry_after() -> None:
    contract = structured_from_enforcer_error(
        MCPRateLimitExceededError("trop de requêtes", retry_after=7)
    )
    assert contract.error_type is MCPErrorType.RATE_LIMITED
    assert contract.effective_retryable is True
    assert contract.retry_after_seconds == 7


# ---------------------------------------------------------------------------
# 3. Timeout — retryable
# ---------------------------------------------------------------------------


def test_gateway_timeout_is_retryable() -> None:
    contract = structured_from_domain_error(GatewayTimeoutError("LLM injoignable"))
    assert contract.error_type is MCPErrorType.TIMEOUT
    assert contract.effective_retryable is True


def test_llm_client_error_is_retryable_timeout() -> None:
    contract = structured_from_domain_error(LLMClientError("provider timeout"))
    assert contract.error_type is MCPErrorType.TIMEOUT
    assert contract.effective_retryable is True


def test_rate_limited_factory_requires_retry_after() -> None:
    data = structured_rate_limited("backpressure", retry_after_seconds=2).to_data()
    assert data["retryable"] is True
    assert data["retryAfterSeconds"] == 2


def test_retry_after_absent_when_no_delay_known() -> None:
    data = MCPStructuredError(MCPErrorType.TIMEOUT, message="timeout").to_data()
    assert data["retryable"] is True
    assert "retryAfterSeconds" not in data


# ---------------------------------------------------------------------------
# 4. Interne — message générique, aucune fuite
# ---------------------------------------------------------------------------


def test_unknown_method_maps_to_not_found_contract() -> None:
    server = _server()
    response = _call(server, json.dumps({"jsonrpc": "2.0", "id": 2, "method": "unknown/method"}))
    assert response["error"]["code"] == ErrorCode.METHOD_NOT_FOUND
    data = _error_data(response)
    assert data["errorType"] == MCPErrorType.NOT_FOUND.value


def test_fallback_internal_error_is_generic() -> None:
    contract = fallback_from_rpc_code(
        ErrorCode.INTERNAL_ERROR, "Traceback (most recent call last): ..."
    )
    assert contract.error_type is MCPErrorType.INTERNAL
    assert contract.message == "Internal error"  # jamais la stack exposée


def test_internal_mapping_never_leaks_domain_message() -> None:
    contract = structured_from_domain_error(RuntimeError("leak /home/user/secrets"))  # type: ignore[arg-type]
    assert contract.error_type is MCPErrorType.INTERNAL
    assert contract.message == "Internal error"


# ---------------------------------------------------------------------------
# 5. Contrat transversal — correlationId, compatibilité, immutabilité
# ---------------------------------------------------------------------------


def test_correlation_id_present_and_unique_per_request() -> None:
    server = _server()
    raw = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "unknown/method"})
    first = _error_data(_call(server, raw))
    second = _error_data(_call(server, raw))
    cid1 = first.get("correlationId")
    cid2 = second.get("correlationId")
    assert isinstance(cid1, str) and cid1
    assert isinstance(cid2, str) and cid2
    assert cid1 != cid2


def test_correlation_id_helper_is_short_hex() -> None:
    cid = new_correlation_id()
    assert len(cid) == 12
    int(cid, 16)  # hex valide


def test_success_responses_are_untouched() -> None:
    server = _server()
    response = _call(server, json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ping"}))
    assert response == {"jsonrpc": "2.0", "id": 4, "result": {}}


def test_error_result_without_data_stays_2_2_compatible() -> None:
    assert error_result(1, ErrorCode.INVALID_PARAMS, "boom") == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": ErrorCode.INVALID_PARAMS, "message": "boom"},
    }


def test_structured_error_is_immutable_and_sanitized_at_construction() -> None:
    contract = MCPStructuredError(
        MCPErrorType.VALIDATION,
        message="échec /workspace/backend/app/x.py",
        field_errors={"path": ["/home/user/data.csv"]},
    )
    assert "[redacted-path]" in contract.message
    assert all("[redacted-path]" in item for item in contract.field_errors["path"])
    with pytest.raises((AttributeError, TypeError)):
        contract.message = "muté"  # type: ignore[misc]


def test_not_found_domain_error_maps_to_not_found_type() -> None:
    contract = structured_from_domain_error(NotFoundError("resource inconnue"))
    assert contract.error_type is MCPErrorType.NOT_FOUND
    assert contract.effective_retryable is False


def test_validation_details_become_field_errors() -> None:
    contract = structured_from_domain_error(
        ValidationError("args invalides", details={"temperature": "doit être un nombre"})
    )
    data = contract.to_data()
    assert data["errorType"] == MCPErrorType.VALIDATION.value
    assert data["fieldErrors"]["temperature"] == ["doit être un nombre"]
