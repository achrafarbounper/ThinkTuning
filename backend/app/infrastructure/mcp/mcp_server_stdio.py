# project/app/infrastructure/mcp/mcp_server_stdio.py
"""Transport stdio du serveur MCP — entry point ``thinktuning-mcp``.

Protocole : un message JSON-RPC 2.0 par ligne sur stdin, une réponse par ligne
sur stdout (convention des serveurs MCP stdio). Toute journalisation part sur
stderr — stdout est RÉSERVÉ au transport (une seule ligne = un seul message,
le client MCP le parse ligne à ligne).

Usage :
    $ echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \\
      | thinktuning-mcp

Le serveur s'arrête proprement à la fin du flux stdin (EOF → exit 0). Le scope
par défaut est ``read_only`` ; la politique CLIENT complète (client store, S4)
choisira le rôle par client.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TextIO

from app.domain.ports.mcp_ports import MCPIdentity
from app.infrastructure.mcp.mcp_audit import audit_mcp_call
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.tenant_isolation import (
    DEFAULT_TENANT_ID,
    sanitize_identity_value,
)

logger = logging.getLogger("thinktuning.mcp.stdio")

# Variables d'environnement portant l'identité du client stdio (MCP 2.3.0 —
# isolation multi-tenant) : le protocole stdio n'a pas d'en-têtes, l'identité
# est donc fournie par l'ENVIRONNEMENT du process lancé par le client MCP.
ENV_CLIENT_ID = "MCP_CLIENT_ID"
ENV_TENANT_ID = "MCP_TENANT_ID"
ENV_SUBJECT_ID = "MCP_SUBJECT_ID"


def resolve_stdio_identity() -> MCPIdentity | None:
    """Identité DÉCLARÉE du client stdio (``None`` si non configurée).

    Le protocole stdio ne transporte pas d'en-têtes : l'identité est portée
    par l'environnement du process (``MCP_CLIENT_ID`` / ``MCP_TENANT_ID`` /
    ``MCP_SUBJECT_ID``), sanitisée selon la même convention que le transport
    SSE. Sans configuration, ``None`` est retourné — comportement 2.2.x
    strictement préservé (aucune garde par client, runs non estampillés).
    """
    raw_client = (os.getenv(ENV_CLIENT_ID) or "").strip()
    raw_subject = (os.getenv(ENV_SUBJECT_ID) or "").strip()
    if not raw_client and not raw_subject:
        return None
    raw_tenant = (os.getenv(ENV_TENANT_ID) or "").strip()
    return MCPIdentity(
        tenant_id=sanitize_identity_value(
            raw_tenant, field="tenant_id", default=DEFAULT_TENANT_ID
        ),
        client_id=sanitize_identity_value(raw_client, field="client_id", default="anonymous"),
        subject_id=(
            sanitize_identity_value(raw_subject, field="subject_id", default="-")
            if raw_subject
            else ""
        ),
    )


def _read_message(stream: TextIO) -> str | None:
    """Lit une ligne JSON-RPC (ignore les lignes vides). ``None`` à l'EOF."""
    for line in stream:
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def serve_stdio(
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    """Boucle stdio : lit stdin ligne à ligne, répond sur stdout.

    Args:
        input_stream:  flux d'entrée (défaut ``sys.stdin``) ;
        output_stream: flux de sortie (défaut ``sys.stdout``).

    Returns:
        Code de sortie : ``0`` (EOF nominal), ``1`` (erreur interne).
    """
    reader = input_stream or sys.stdin
    writer = output_stream or sys.stdout
    # Tâche 12 : le transport stdio audite aussi (client_id anonyme — le
    # protocole stdio ne porte pas d'identité client).
    server = build_mcp_server(audit=audit_mcp_call)
    # MCP 2.3.0 (SCRUM-161) : identité déclarée par l'ENVIRONNEMENT (le
    # protocole stdio n'a pas d'en-têtes) — ``None`` → comportement 2.2.x.
    identity = resolve_stdio_identity()
    if identity is not None:
        logger.info(
            "MCP stdio identifié : tenant=%s client=%s",
            identity.tenant_id,
            identity.client_id,
        )
    try:
        while True:
            raw = _read_message(reader)
            if raw is None:  # EOF : fin du transport
                return 0
            response = server.handle_text(
                raw,
                client_id=identity.client_id if identity is not None else "anonymous",
                identity=identity,
            )
            if response is not None:
                writer.write(response + "\n")
                writer.flush()
    except Exception as exc:  # fail-closed : ne JAMAIS crasher le client
        logger.exception("Erreur interne du serveur MCP stdio : %s", exc)
        return 1


def main() -> int:
    """Entry point console ``thinktuning-mcp``."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    return serve_stdio()


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "resolve_stdio_identity", "serve_stdio"]
