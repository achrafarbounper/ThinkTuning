"""Exemple EXÉCUTABLE — contrats STREAM et REPLAY d'un run MCP durable (L4, SCRUM-155).

Ce script est la version exécutable des sections « Contrat de stream » et
« Contrat de replay » de ``docs/mcp/MULTI_AGENT_SSE_FLOW.md``. Il ne nécessite
NI serveur, NI LLM, NI MongoDB : l'orchestrateur est un double déterministe et
le store durable est un SQLite temporaire — le comportement observé est donc
reproductible en local et en CI.

Ce qu'il démontre, dans l'ordre :

1. **Prélude** : ``prepare_run`` crée le run durable AVANT le premier octet
   (``orchestrate.started`` expose ``run_id``, ``resumed``, ``last_sequence``) ;
2. **Contrat de stream** : chaque événement relayé porte sa ``sequence``
   persistée (curseur de reprise mémorisable par le client) ; les frames SSE
   sont imprimées au format exact du transport ;
3. **Contrat de replay** : après une coupure simulée au curseur ``k``,
   ``orchestrate_events(after_sequence=k)`` réémet uniquement les événements
   POSTÉRIEURS — sans rejouer l'historique, et avec un nouveau curseur ;
4. **FSM durable** : l'état final (``state``/``phase``/``checkpoint``/
   ``last_sequence``) est interrogé via ``orchestrate_get_run``.

Usage :

    cd backend
    python scripts/example_mcp_stream_replay.py

Références : ``docs/mcp/MULTI_AGENT_SSE_FLOW.md`` (§ contrats), ``app/
application/mcp_orchestration.py`` (adaptateur), ``app/infrastructure/
mcp/tools/orchestrate_tool.py`` (outil ``orchestrate``/``orchestrate_events``).
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.application.mcp_orchestration import MultiAgentMCPAdapter  # noqa: E402
from app.domain.ports import MCPOrchestrationRequest  # noqa: E402
from app.infrastructure.persistence.mcp_run_store import MCPDurableRunStore  # noqa: E402

#: Sentinelle de fin de flux (contrat front : sortie sur sentinelle).
SSE_END_SENTINEL = "data: [DONE]\n\n"

PROMPT = "Comparer les resultats des deux modeles"

#: Séquence à partir de laquelle on simule une coupure client (replay incrémental).
DISCONNECT_AFTER_SEQUENCE = 2


def sse_frame(kind: str, payload: dict[str, Any]) -> str:
    """Sérialise un événement nommé au format EXACT du transport SSE."""
    return f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class DemoOrchestrator:
    """Double déterministe de ``MultiAgentOrchestratorPort`` (aucun réseau).

    Émet la hiérarchie réelle d'un run multi-agent — ``lead`` (plan) →
    ``worker`` → ``synthesis`` — puis retourne un résultat au contrat stable.
    """

    def run_streaming(
        self,
        prompt: str,
        *,
        model: str | None = None,
        parallel: bool = False,
        resume_request_id: str | None = None,
        enable_thinking: bool = False,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        events: list[tuple[str, dict[str, Any]]] = [
            ("agent.plan", {"plan": [{"task_id": "w-1", "role": "analyst", "subtask": prompt}]}),
            ("agent.worker.start", {"worker_id": "w-1", "task_id": "w-1", "status": "running"}),
            ("agent.worker.tool", {"worker_id": "w-1", "task_id": "w-1", "tool": "dataset_stats"}),
            ("agent.worker.result", {"worker_id": "w-1", "task_id": "w-1", "status": "ok"}),
            ("agent.synthesizing", {"phase": "synthesis"}),
            ("agent.done", {"phase": "synthesis", "final_answer": "Comparaison terminee."}),
        ]
        for kind, payload in events:
            if on_event is not None:
                on_event(kind, payload)
        return {
            "final_answer": "Comparaison terminee.",
            "status": "completed",
            "lead": {"status": "completed", "plan": [{"task_id": "w-1"}]},
            "workers": [{"task_id": "w-1", "worker_id": "w-1", "status": "ok"}],
            "synthesis": {"status": "completed"},
            "usage": {"rounds": 2, "tool_calls": 1},
        }

    def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        parallel: bool = False,
        resume_request_id: str | None = None,
        enable_thinking: bool = False,
    ) -> dict[str, Any]:
        return self.run_streaming(
            prompt,
            model=model,
            parallel=parallel,
            resume_request_id=resume_request_id,
            enable_thinking=enable_thinking,
        )


def _section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="thinktuning-mcp-example-"))
    store = MCPDurableRunStore(workdir / "mcp-runs.db")
    adapter = MultiAgentMCPAdapter(DemoOrchestrator(), store)

    # ------------------------------------------------------------------
    # 1. Prélude : le run durable existe AVANT le premier octet utile
    # ------------------------------------------------------------------
    _section("1. Prelude orchestrate.started (run durable prepare avant le 1er octet)")
    prepared = adapter.prepare_run(
        MCPOrchestrationRequest.from_values(prompt=PROMPT, parallel=True)
    )
    assert prepared is not None, "un store durable est requis pour l'exemple"
    run_id = str(prepared["run_id"])
    print(
        sse_frame(
            "orchestrate.started",
            {
                "status": "started",
                "mode": "multi_agent",
                "run_id": run_id,
                "resumed": False,
                "last_sequence": int(prepared.get("last_sequence") or 0),
            },
        ),
        end="",
    )

    # ------------------------------------------------------------------
    # 2. Contrat de stream : chaque événement relayé porte sa sequence
    # ------------------------------------------------------------------
    _section("2. Contrat de stream (sequence persistee = curseur de reprise)")
    streamed: list[dict[str, Any]] = []

    def on_event(kind: str, payload: dict[str, Any]) -> None:
        streamed.append({"kind": kind, **payload})
        print(sse_frame(kind, payload), end="")

    request = MCPOrchestrationRequest.from_values(prompt=PROMPT, parallel=True, run_id=run_id)
    result = adapter.run(request, on_event=on_event)
    print(SSE_END_SENTINEL, end="")

    observed_cursor = max(int(item.get("sequence") or 0) for item in streamed)
    print(f"-> run_id={run_id} status={result.status} evenements_relayes={len(streamed)}")
    print(f"-> curseur memorise par le client (last_sequence) = {observed_cursor}")

    # ------------------------------------------------------------------
    # 3. Contrat de replay : reprise INCREMENTALE apres coupure
    # ------------------------------------------------------------------
    _section(
        f"3. Contrat de replay (coupure simulee apres la sequence {DISCONNECT_AFTER_SEQUENCE})"
    )
    print(
        sse_frame(
            "replay_started", {"run_id": run_id, "after_sequence": DISCONNECT_AFTER_SEQUENCE}
        ),
        end="",
    )
    replayed = adapter.get_events(run_id, after_sequence=DISCONNECT_AFTER_SEQUENCE)
    for event in replayed:
        print(sse_frame("orchestrate.replay", event), end="")
    new_cursor = max(
        [int(event.get("sequence") or 0) for event in replayed] or [DISCONNECT_AFTER_SEQUENCE]
    )
    print(sse_frame("replay_completed", {"run_id": run_id, "last_sequence": new_cursor}), end="")
    print(SSE_END_SENTINEL, end="")
    print(f"-> evenements rejoues={len(replayed)} nouveau_curseur={new_cursor}")
    print("-> un replay ulterieur repart de ce curseur : aucun historique rejoue")

    # ------------------------------------------------------------------
    # 4. FSM durable : etat final interrogeable (orchestrate_get_run)
    # ------------------------------------------------------------------
    _section("4. Etat durable final (orchestrate_get_run)")
    snapshot = adapter.get_run(run_id) or {}
    print(
        json.dumps(
            {
                "run_id": snapshot.get("run_id"),
                "state": snapshot.get("state"),
                "phase": snapshot.get("phase"),
                "checkpoint": snapshot.get("checkpoint"),
                "last_sequence": snapshot.get("last_sequence"),
                "retry_count": snapshot.get("retry_count"),
                "lease_owner": snapshot.get("lease_owner"),
                "persisted_events": len(snapshot.get("events") or []),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )

    # Invariants du contrat — vérifiés à l'exécution (l'exemple est un test vivant).
    assert result.run_id == run_id, "le run durable doit conserver son run_id"
    assert observed_cursor > 0, "chaque evenement stream doit porter une sequence"
    assert all(int(event.get("sequence") or 0) > DISCONNECT_AFTER_SEQUENCE for event in replayed), (
        "le replay ne doit reemettre que les evenements POSTERIEURS au curseur"
    )
    assert new_cursor >= observed_cursor, "le curseur de replay est monotone"
    assert snapshot.get("state") == "completed"
    assert snapshot.get("lease_owner") is None, "le lease doit etre libere en fin de run"

    print()
    print("OK - contrats stream et replay verifies (docs/mcp/MULTI_AGENT_SSE_FLOW.md).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
