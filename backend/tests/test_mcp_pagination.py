"""MCP 2.3.0 — pagination par curseur OPAQUE des catalogues.

Couverture des critères d'acceptation :
    1. pagination par curseur sur tools/list, resources/list, prompts/list ;
    2. curseur OPAQUE (renvoyé tel quel, non décodable côté client) ;
    3. taille de page configurable (``page_size`` + env ``MCP_PAGINATION_PAGE_SIZE``) ;
    4. compatibilité : sans curseur, réponse identique aux versions 2.2.x ;
    5. curseurs invalides / falsifiés / croisés / non-string → Invalid params ;
    6. EXPIRATION des curseurs (TTL configuré, défaut 900 s).
"""

from __future__ import annotations

import base64
import json

import pytest

from app.domain.entities.mcp import MCPResource, MCPScopeRole, MCPTool, MCPVersion
from app.infrastructure.mcp.catalog_pagination import (
    CursorError,
    decode_cursor,
    encode_cursor,
)
from app.infrastructure.mcp.mcp_server import InMemoryToolProvider, MCPServer
from app.infrastructure.mcp.protocol import ErrorCode, empty_input_schema


def _tool(name: str) -> MCPTool:
    return MCPTool(
        name=name,
        description=f"test tool {name}",
        input_schema=empty_input_schema(),
        annotations={"readOnlyHint": True},
        required_scope=MCPScopeRole.READ_ONLY,
        handler=lambda arguments: "ok",
    )


class _StaticResourceProvider:
    """Port ``MCPResourceRegistryPort`` minimal : N resources statiques."""

    def __init__(self, count: int) -> None:
        self._resources = [
            MCPResource(uri=f"thinktuning://item/{index}", name=f"item_{index}")
            for index in range(count)
        ]

    def list_resources(self) -> list[MCPResource]:
        return list(self._resources)

    def read_resource(self, uri: str) -> str:  # pragma: no cover - non paginé ici
        return "static"


class _StaticPromptProvider:
    """Port ``MCPPromptRegistryPort`` minimal : N prompts statiques."""

    def __init__(self, count: int) -> None:
        from app.domain.entities.mcp import MCPPromptTemplate

        self._prompts = [
            MCPPromptTemplate(name=f"prompt_{index}", description=f"p{index}")
            for index in range(count)
        ]

    def list_prompts(self) -> list:
        return list(self._prompts)

    def get_prompt(self, name: str, arguments: dict):  # pragma: no cover - non paginé
        from app.domain.errors import NotFoundError

        raise NotFoundError(f"unknown prompt: {name}")


def _catalog_server(page_size: int | None = None, *, tools: int = 7) -> MCPServer:
    kwargs: dict = {
        "version": MCPVersion.parse("2.3.0"),
        "scope": MCPScopeRole.READ_ONLY,
        "tool_provider": InMemoryToolProvider([_tool(f"tool_{i}") for i in range(tools)]),
        "resource_provider": _StaticResourceProvider(5),
        "prompt_provider": None,
    }
    if page_size is not None:
        kwargs["page_size"] = page_size
    return MCPServer(**kwargs)


def _call(server: MCPServer, method: str, request_id: int = 1, **params: object) -> dict:
    return json.loads(server.handle_text(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    ))


def _walk(server: MCPServer, method: str, key: str, **first_params: object) -> tuple[list, int]:
    """Itère un catalogue page à page → (noms/uris dans l'ordre, nb pages)."""
    items: list = []
    params = dict(first_params)
    pages = 0
    while True:
        reply = _call(server, method, pages + 1, **params)
        assert "error" not in reply, reply
        items.extend(entry.get("name") or entry.get("uri") for entry in reply["result"][key])
        pages += 1
        cursor = reply["result"].get("nextCursor")
        if cursor is None:
            return items, pages
        assert pages < 20  # garde-fou : pas de boucle infinie
        params = {"cursor": cursor}


# --- 1. Pagination par curseur (les 3 catalogues) --------------------------------


def test_tools_list_paginates_and_exhausts_cursor_chain():
    server = _catalog_server(page_size=3)
    names, pages = _walk(server, "tools/list", "tools")
    assert names == [f"tool_{i}" for i in range(7)]  # ordre préservé, pas de perte
    assert pages == 3  # 3 + 3 + 1


def test_resources_list_paginates_and_exhausts_cursor_chain():
    server = _catalog_server(page_size=2)
    uris, pages = _walk(server, "resources/list", "resources")
    assert uris == [f"item_{i}" for i in range(5)]
    assert pages == 3  # 2 + 2 + 1


def test_prompts_list_paginates_and_exhausts_cursor_chain():
    server = MCPServer(
        version=MCPVersion.parse("2.3.0"),
        scope=MCPScopeRole.READ_ONLY,
        tool_provider=InMemoryToolProvider([]),
        resource_provider=None,
        prompt_provider=_StaticPromptProvider(5),
        page_size=2,
    )
    names, pages = _walk(server, "prompts/list", "prompts")
    assert names == [f"prompt_{i}" for i in range(5)]
    assert pages == 3  # 2 + 2 + 1


def test_empty_surface_catalogs_page_to_a_single_page():
    server = MCPServer(
        version=MCPVersion.parse("2.3.0"),
        scope=MCPScopeRole.READ_ONLY,
        tool_provider=InMemoryToolProvider([]),
        resource_provider=None,
        prompt_provider=None,
        page_size=3,
    )
    for method, key in (("tools/list", "tools"), ("resources/list", "resources"),
                        ("prompts/list", "prompts")):
        reply = _call(server, method)
        assert reply["result"] == {key: []}
        assert "nextCursor" not in reply["result"]


# --- 2. Curseur opaque ------------------------------------------------------------


def test_cursor_is_opaque_no_plaintext_and_signed():
    server = _catalog_server(page_size=3)
    cursor = _call(server, "tools/list")["result"]["nextCursor"]
    assert cursor and cursor.count(".") == 1
    # « Opaque » : le jeton transmis n'est ni du JSON ni de l'URL-exploitable —
    # base64url(payload) + HMAC, que le client renvoie tel quel sans l'ouvrir.
    payload, _signature = cursor.split(".")
    assert "{" not in cursor and "tools" not in cursor
    assert "=" not in cursor  # canonical base64url sans padding
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    assert decoded.startswith(b'{"k":')  # charge utile uniquement côté serveur


def test_tampered_cursor_is_rejected():
    server = _catalog_server(page_size=3)
    cursor = _call(server, "tools/list")["result"]["nextCursor"]
    body, signature = cursor.split(".")
    forged = f"{body}.{'A' * len(signature)}"  # même payload, signature cassée
    reply = _call(server, "tools/list", 2, cursor=forged)
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS
    assert "signature" in reply["error"]["message"]


def test_foreign_secret_cursor_is_rejected():
    server = _catalog_server(page_size=3)
    forged = encode_cursor("tools", 3, secret=b"foreign-secret")
    reply = _call(server, "tools/list", 2, cursor=forged)
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS


# --- 3. Taille de page configurable ----------------------------------------------


def test_page_size_is_configurable_per_server():
    server = _catalog_server(page_size=5)
    reply = _call(server, "tools/list")
    assert len(reply["result"]["tools"]) == 5
    assert "nextCursor" in reply["result"]


def test_page_size_configurable_via_environment(monkeypatch):
    monkeypatch.setenv("MCP_PAGINATION_PAGE_SIZE", "4")
    server = _catalog_server()
    reply = _call(server, "tools/list")
    assert len(reply["result"]["tools"]) == 4
    # page_size explicite > env.
    server = _catalog_server(page_size=2)
    assert len(_call(server, "tools/list")["result"]["tools"]) == 2


def test_page_size_invalid_environment_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MCP_PAGINATION_PAGE_SIZE", "not-an-int")
    server = _catalog_server()
    result = _call(server, "tools/list")["result"]
    assert len(result["tools"]) == 7  # défaut 50 > 7 tools : tout est rendu


def test_page_size_below_one_is_clamped_to_one():
    server = _catalog_server(page_size=0)
    assert len(_call(server, "tools/list")["result"]["tools"]) == 1


# --- 4. Compatibilité (sans curseur, comportement 2.2.x) --------------------------


def test_no_cursor_first_page_compatible_with_default_page_size():
    """Sans curseur ni page_size : réponse identique à 2.2.x (pas de nextCursor)."""
    server = _catalog_server()  # défaut 50 > 7 tools
    for method, key in (("tools/list", "tools"), ("resources/list", "resources")):
        reply = _call(server, method)
        assert "error" not in reply, reply
        assert "nextCursor" not in reply["result"]
        assert isinstance(reply["result"][key], list) and reply["result"][key]


def test_explicit_cursor_param_on_catalog_round_trips():
    """Le curseur renvoyé est réutilisable tel quel (contrat opaque MCP)."""
    server = _catalog_server(page_size=3)
    first = _call(server, "tools/list")["result"]
    second = _call(server, "tools/list", 2, cursor=first["nextCursor"])
    assert [t["name"] for t in second["result"]["tools"]] == [
        f"tool_{i}" for i in range(3, 6)
    ]


# --- 5. Curseurs invalides --------------------------------------------------------


@pytest.mark.parametrize("bad_cursor", [
    "garbage",
    "..",
    ".",
    "AAAA.BBBB",  # base64 valide, signature invalide
    "eyJ2IjoxfQ.not-a-real-signature",  # payload plausible, signé par personne
])
def test_invalid_cursors_are_rejected_with_invalid_params(bad_cursor: str):
    server = _catalog_server(page_size=3)
    reply = _call(server, "tools/list", 2, cursor=bad_cursor)
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS


def test_non_string_cursor_is_rejected():
    server = _catalog_server(page_size=3)
    reply = _call(server, "tools/list", 2, cursor=42)
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS


def test_cross_catalog_cursor_is_rejected():
    """Un curseur tools/list rejoué sur resources/list → Invalid params."""
    server = _catalog_server(page_size=2)
    tools_cursor = _call(server, "tools/list")["result"]["nextCursor"]
    reply = _call(server, "resources/list", 2, cursor=tools_cursor)
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS
    assert "resources" in reply["error"]["message"]


def test_unit_decode_cursor_cross_kind_raises_not_expired():
    with pytest.raises(CursorError) as excinfo:
        decode_cursor(encode_cursor("tools", 3), "prompts")
    assert not excinfo.value.expired


# --- 6. Expiration des curseurs ---------------------------------------------------


def test_expired_cursor_is_detected_by_unit_decode():
    server = _catalog_server(page_size=3)
    cursor = _call(server, "tools/list")["result"]["nextCursor"]
    offset = decode_cursor(cursor, "tools")  # valide maintenant
    assert offset == 3
    with pytest.raises(CursorError) as excinfo:
        decode_cursor(cursor, "tools", now=decode_now_after_ttl(cursor))
    assert excinfo.value.expired


def decode_now_after_ttl(cursor: str) -> float:
    """Horloge « 1 h plus tard » : dernière émission + TTL 900 s + marge."""
    import time as _time

    return _time.time() + 3600.0


def test_expired_cursor_via_server_replies_invalid_params(monkeypatch):
    import time as _time

    monkeypatch.setenv("MCP_PAGINATION_CURSOR_TTL_SECONDS", "1")
    server = _catalog_server(page_size=3)
    cursor = _call(server, "tools/list")["result"]["nextCursor"]
    _time.sleep(1.1)
    reply = _call(server, "tools/list", 2, cursor=cursor)
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS
    assert "expired" in reply["error"]["message"]


def test_fresh_cursor_within_ttl_is_accepted(monkeypatch):
    import time as _time

    monkeypatch.setenv("MCP_PAGINATION_CURSOR_TTL_SECONDS", "60")
    server = _catalog_server(page_size=3)
    cursor = _call(server, "tools/list")["result"]["nextCursor"]
    _time.sleep(0.05)
    reply = _call(server, "tools/list", 2, cursor=cursor)
    assert "error" not in reply, reply
