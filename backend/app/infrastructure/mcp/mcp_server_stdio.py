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
import sys
from typing import TextIO

from app.infrastructure.mcp.mcp_audit import audit_mcp_call
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server

logger = logging.getLogger("thinktuning.mcp.stdio")


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
    try:
        while True:
            raw = _read_message(reader)
            if raw is None:  # EOF : fin du transport
                return 0
            response = server.handle_text(raw)
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


__all__ = ["main", "serve_stdio"]
