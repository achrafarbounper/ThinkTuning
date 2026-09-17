# project/tests/test_mcp_sse_resume.py
"""Tests de RECONNEXION SSE — reprise des événements d'un run durable.

MCP 2.3.0 (SCRUM-163) : un client reconnecté reprend le flux d'un run durable
à partir d'un IDENTIFIANT (``run_id`` + ``after_sequence``, L1 SCRUM-152) ou
d'un CURSEUR de reprise opaque (``resume_token``), ou encore de l'en-tête SSE
natif ``Last-Event-ID``.

Critères d'acceptation couverts :
    1. ABSENCE DE DOUBLONS — le watermark monotone du replay garantit qu'un
       événement déjà consommé n'est JAMAIS ré-émis ;
    2. ORDRE CONSERVÉ — les séquences émises sont strictement croissantes ;
    3. CURSEUR EXPIRÉ — erreur explicite ``replay.error`` +
       ``error_code=resume_token_expired`` (comportement documenté §7),
       jamais de repli silencieux ;
    4. TESTS DE RECONNEXION — token expiré / falsifié / croisé, run inconnu,
       ``Last-Event-ID``, mode ``follow`` (drain live + timeout).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from app.infrastructure.mcp import mcp_server_sse, resume_cursor
from app.infrastructure.persistence.mcp_run_store import MCPDurableRunStore

# Poll/timeout raccourcis : les tests du mode follow tournent en ~0.1-0.4 s.
FOLLOW_POLL_SECONDS = 0.02
FOLLOW_TIMEOUT_SECONDS = 0.4


def _chunks(gen) -> list[str]:
    async def _run() -> list[str]:
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


def _parse(chunks: list[str]) -> list[tuple[str, dict[str, Any], int | None]]:
    """Parse les blocs SSE nommés — retourne ``(event, data, id)``."""
    out: list[tuple[str, dict[str, Any], int | None]] = []
    for chunk in chunks:
        event = "message"
        data = ""
        sse_id: int | None = None
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip() or "message"
            elif line.startswith("data:"):
                data = line[len("data:"):].lstrip()
            elif line.startswith("id:"):
                sse_id = int(line[len("id:"):].strip())
        if not data or data == "[DONE]":
            continue
        out.append((event, json.loads(data), sse_id))
    return out


def _replay_payload(**arguments: Any) -> dict[str, Any]:
    return {
        "params": {
            "arguments": {"replay": True, "stream": True, **arguments},
        }
    }


def _make_store(tmp_path) -> MCPDurableRunStore:
    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    store.create("run-1")
    return store


@pytest.fixture
def durable_store(tmp_path, monkeypatch) -> MCPDurableRunStore:
    """Store SQLite éphémère injecté dans le transport SSE (restauré après)."""
    store = _make_store(tmp_path)
    mcp_server_sse.configure_mcp_durable_run_store(store)
    monkeypatch.setenv("MCP_RESUME_FOLLOW_POLL_SECONDS", str(FOLLOW_POLL_SECONDS))
    monkeypatch.setenv("MCP_RESUME_FOLLOW_TIMEOUT_SECONDS", str(FOLLOW_TIMEOUT_SECONDS))
    yield store
    mcp_server_sse.configure_mcp_durable_run_store(None)


_SSE_API_KEY = "test-mcp-resume-key"
_SSE_AUTH = {"X-API-Key": _SSE_API_KEY}


@pytest.fixture
def sse_client(monkeypatch):
    """Mini-app FastAPI ne montant QUE le router MCP SSE (même motif que test_mcp_flow)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.infrastructure.mcp.mcp_server_sse import router as mcp_sse_router

    monkeypatch.setenv("API_KEY", _SSE_API_KEY)
    app = FastAPI()
    app.include_router(mcp_sse_router)
    return TestClient(app)



def _append(store: MCPDurableRunStore, event: str, **extra: Any) -> int:
    return store.append_event("run-1", {"event": event, **extra})


# ---------------------------------------------------------------------------
# 1. Reprise par IDENTIFIANT (L1 — SCRUM-152, anti-régression) + invariants
# ---------------------------------------------------------------------------


def test_replay_by_identifier_preserves_order_and_no_duplicates(durable_store) -> None:
    """Reprise par identifiant : ordre croissant strict, aucun doublon."""
    for index in range(1, 5):
        _append(durable_store, f"worker.step-{index}", index=index)

    parsed = _parse(
        _chunks(mcp_server_sse._replay_durable_events(_replay_payload(run_id="run-1")))
    )
    replays = [
        (event, data, sse_id) for event, data, sse_id in parsed if event == "orchestrate.replay"
    ]
    sequences = [sse_id for _, _, sse_id in replays]
    assert sequences == [1, 2, 3, 4]
    payload_sequences = [data["sequence"] for _, data, _ in replays]
    assert payload_sequences == sequences  # id: == payload.sequence (curseur unifié)
    completed = next(data for event, data, _ in parsed if event == "replay_completed")
    assert completed["last_sequence"] == 4
    # Le client reçoit un CURSEUR pour la prochaine reconnexion.
    assert completed["resume_token"]
    run_id, sequence = resume_cursor.decode_resume_token(completed["resume_token"])
    assert (run_id, sequence) == ("run-1", 4)


def test_resume_from_identifier_replays_only_missing_events(durable_store) -> None:
    """Reprise après coupure : les événements <= curseur ne sont JAMAIS ré-émis."""
    for index in range(1, 5):
        _append(durable_store, f"worker.step-{index}", index=index)

    first = _parse(
        _chunks(
            mcp_server_sse._replay_durable_events(_replay_payload(run_id="run-1", after_sequence=2))
        )
    )
    first_sequences = [sse_id for event, _, sse_id in first if event == "orchestrate.replay"]
    assert first_sequences == [3, 4]

    # Nouveaux événements produits après la reprise du client.
    _append(durable_store, "orchestrate.done")
    second = _parse(
        _chunks(
            mcp_server_sse._replay_durable_events(_replay_payload(run_id="run-1", after_sequence=4))
        )
    )
    second_sequences = [sse_id for event, _, sse_id in second if event == "orchestrate.replay"]
    assert second_sequences == [5]
    # Union des deux flux : AUCUN doublon (critère d'acceptation n°1).
    union = first_sequences + second_sequences
    assert len(union) == len(set(union))


def test_replay_rejects_negative_sequence(durable_store) -> None:
    """Curseur négatif : erreur explicite (rejouer TOUT dupliquerait l'historique)."""
    parsed = _parse(
        _chunks(
            mcp_server_sse._replay_durable_events(
                _replay_payload(run_id="run-1", after_sequence=-3)
            )
        )
    )
    assert parsed[0][0] == "replay.error"
    assert parsed[0][1]["error_code"] == "after_sequence_invalid"
    assert parsed[0][1]["error"] == "after_sequence must be >= 0"


def test_replay_rejects_non_integer_sequence(durable_store) -> None:
    """L1 préservé : ``after_sequence`` non entier → message inchangé."""
    parsed = _parse(
        _chunks(
            mcp_server_sse._replay_durable_events(
                _replay_payload(run_id="run-1", after_sequence="invalid")
            )
        )
    )
    assert parsed[0][0] == "replay.error"
    assert parsed[0][1]["error_code"] == "after_sequence_invalid"
    assert parsed[0][1]["error"] == "after_sequence must be an integer"


def test_replay_rejects_missing_run_identifier(durable_store) -> None:
    parsed = _parse(_chunks(mcp_server_sse._replay_durable_events(_replay_payload())))
    assert parsed[0][0] == "replay.error"
    assert parsed[0][1]["error_code"] == "run_id_required"


def test_replay_always_closes_with_done(durable_store) -> None:
    """Tous les chemins (nominal ET erreur) se clôturent par ``data: [DONE]``."""
    for arguments in (
        {"run_id": "run-1"},
        {"run_id": "run-1", "after_sequence": "boom"},
        {},
    ):
        chunks = _chunks(mcp_server_sse._replay_durable_events(_replay_payload(**arguments)))
        assert chunks[-1] == "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# 2. Reprise par CURSEUR (resume_token — MCP 2.3.0)
# ---------------------------------------------------------------------------


def test_resume_token_roundtrip_resumes_without_duplicates(durable_store) -> None:
    """Un client reconnecté rejoue le token : uniquement les événements manquants."""
    for index in range(1, 4):
        _append(durable_store, f"worker.step-{index}", index=index)
    first = _parse(
        _chunks(mcp_server_sse._replay_durable_events(_replay_payload(run_id="run-1")))
    )
    started = next(data for event, data, _ in first if event == "replay_started")
    assert started["resume_token_ttl_seconds"] == resume_cursor.token_ttl_seconds()

    completed = next(data for event, data, _ in first if event == "replay_completed")
    # Côté serveur : de nouveaux événements sont produits pendant la coupure.
    _append(durable_store, "orchestrate.synthesis")
    _append(durable_store, "orchestrate.done")

    resumed = _parse(
        _chunks(
            mcp_server_sse._replay_durable_events(
                _replay_payload(resume_token=completed["resume_token"])
            )
        )
    )
    replayed = [data for event, data, _ in resumed if event == "orchestrate.replay"]
    assert [data["event"] for data in replayed] == [
        "orchestrate.synthesis",
        "orchestrate.done",
    ]
    # L'historique déjà consommé (séquences 1..3) n'est JAMAIS re-émis.
    assert all(data["sequence"] > 3 for data in replayed)
    completed2 = next(data for event, data, _ in resumed if event == "replay_completed")
    assert completed2["last_sequence"] == 5


def test_resume_token_rejects_run_mismatch(durable_store) -> None:
    """Token d'un AUTRE run : rejet explicite (anti-re-jeu croisé)."""
    other_token = resume_cursor.encode_resume_token("run-2", 7)
    chunks = _chunks(
        mcp_server_sse._replay_durable_events(
            _replay_payload(run_id="run-1", resume_token=other_token)
        )
    )
    parsed = _parse(chunks)
    assert parsed[0][0] == "replay.error"
    assert parsed[0][1]["error_code"] == "resume_token_invalid"
    assert "run-2" in parsed[0][1]["error"]
    assert chunks[-1] == "data: [DONE]\n\n"


def test_resume_token_expired_is_documented(durable_store, monkeypatch) -> None:
    """CRITÈRE n°3 : curseur expiré → erreur EXPLICITE, comportement documenté."""
    expired_token = resume_cursor.encode_resume_token(
        "run-1", 2, issued_at=time.time() - 10_000
    )
    chunks = _chunks(
        mcp_server_sse._replay_durable_events(_replay_payload(resume_token=expired_token))
    )
    parsed = _parse(chunks)
    assert parsed[0][0] == "replay.error"
    assert parsed[0][1]["error_code"] == "resume_token_expired"
    assert "expired" in parsed[0][1]["error"]
    # Le flux se clôture proprement : le client documenté reprend ensuite avec
    # run_id + after_sequence (séquence persistée, elle, ne périt jamais).
    assert chunks[-1] == "data: [DONE]\n\n"
    assert not any(event == "replay_started" for event, _, _ in parsed)


def test_resume_token_tampered_signature_is_rejected(durable_store) -> None:
    """Token falsifié (signature invalide) : rejet explicite, jamais de replay."""
    token = resume_cursor.encode_resume_token("run-1", 2)
    body, signature = token.split(".")
    tampered = f"{body}{'0' if signature[0] != '0' else '1'}.{signature}"
    parsed = _parse(
        _chunks(
            mcp_server_sse._replay_durable_events(_replay_payload(resume_token=tampered))
        )
    )
    assert parsed[0][1]["error_code"] == "resume_token_invalid"


def test_resume_token_malformed_is_rejected(durable_store) -> None:
    parsed = _parse(
        _chunks(mcp_server_sse._replay_durable_events(_replay_payload(resume_token="garbage")))
    )
    assert parsed[0][1]["error_code"] == "resume_token_invalid"


def test_resume_token_from_foreign_domain_is_rejected(durable_store) -> None:
    """Un curseur de PAGINATION (autre domaine HMAC) n'est pas un curseur de reprise."""
    from app.infrastructure.mcp.catalog_pagination import KIND_TOOLS, encode_cursor

    foreign = encode_cursor(KIND_TOOLS, 3)
    chunks = _chunks(
        mcp_server_sse._replay_durable_events(_replay_payload(resume_token=foreign))
    )
    parsed = _parse(chunks)
    assert parsed[0][1]["error_code"] == "resume_token_invalid"
    assert chunks[-1] == "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# 3. Reconnexion SSE native (en-tête Last-Event-ID)
# ---------------------------------------------------------------------------


def test_last_event_id_header_resumes_stream(durable_store, sse_client) -> None:
    """Un EventSource reconnecté renvoie ``Last-Event-ID`` : reprise honorée."""
    response = sse_client.post(
        "/mcp/sse",
        content=json.dumps({
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "orchestrate_events",
                "arguments": {"run_id": "run-1", "stream": True},
            },
        }),
        headers={**_SSE_AUTH, "Last-Event-ID": "7"},
    )
    assert response.status_code == 200
    # Le curseur du header devient l'after_sequence du replay (aucun doublon).
    assert '"after_sequence": 7' in response.text
    assert '"error_code"' not in response.text.split("replay_started")[0]
    assert "data: [DONE]" in response.text


def test_last_event_id_header_is_ignored_when_after_sequence_given(
    durable_store, sse_client
) -> None:
    """Le curseur EXPLICITE prime sur l'en-tête (pas d'ambiguïté de priorité)."""
    response = sse_client.post(
        "/mcp/sse",
        content=json.dumps({
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "orchestrate_events",
                "arguments": {"run_id": "run-1", "after_sequence": 2, "stream": True},
            },
        }),
        headers={**_SSE_AUTH, "Last-Event-ID": "9"},
    )
    assert response.status_code == 200
    assert '"after_sequence": 2' in response.text


def test_replayed_events_carry_native_sse_ids(durable_store) -> None:
    """CRITÈRE reconnexion : chaque événement rejoué porte sa séquence en ``id:``."""
    _append(durable_store, "orchestrate.start")
    _append(durable_store, "worker.result")
    chunks = _chunks(
        mcp_server_sse._replay_durable_events(_replay_payload(run_id="run-1"))
    )
    replay_chunks = [chunk for chunk in chunks if chunk.startswith("id: ")]
    ids = [int(chunk.splitlines()[0][len("id: "):]) for chunk in replay_chunks]
    assert ids == [1, 2]  # séquences strictement croissantes (ordre conservé)


# ---------------------------------------------------------------------------
# 4. Mode follow : reprise d'un run ENCORE EN COURS (reconnexion vivante)
# ---------------------------------------------------------------------------


def test_follow_drains_live_events_until_run_settles(durable_store) -> None:
    """Le run produit des événements PENDANT la reprise : drainé une seule fois."""
    durable_store.transition("run-1", "running")
    _append(durable_store, "orchestrate.start")
    async def scenario() -> list[tuple[str, dict[str, Any], int | None]]:
        async def produce() -> None:
            # Arrive ENTRE deux polls (poll = 0.02 s) : la reprise doit les
            # capter, dans l'ordre, sans doublon ni événement manquant.
            await asyncio.sleep(0.05)
            _append(durable_store, "worker.result")
            await asyncio.sleep(0.05)
            _append(durable_store, "orchestrate.done")
            durable_store.transition("run-1", "completed", checkpoint="completed")

        producer = asyncio.create_task(produce())
        chunks = [chunk async for chunk in mcp_server_sse._replay_durable_events(
            _replay_payload(run_id="run-1", follow=True)
        )]
        await producer
        return _parse(chunks)

    parsed = asyncio.run(scenario())
    replays = [
        (data["event"], data["sequence"])
        for event, data, _ in parsed
        if event == "orchestrate.replay"
    ]
    assert replays == [
        ("orchestrate.start", 1),
        ("worker.result", 2),
        ("orchestrate.done", 3),
    ]
    completed = next(data for event, data, _ in parsed if event == "replay_completed")
    assert completed["last_sequence"] == 3
    assert "follow_timed_out" not in completed


def test_follow_always_closes_with_done(durable_store) -> None:
    durable_store.transition("run-1", "running")
    _append(durable_store, "orchestrate.start")

    chunks = _chunks(
        mcp_server_sse._replay_durable_events(_replay_payload(run_id="run-1", follow=True))
    )
    assert chunks[-1] == "data: [DONE]\n\n"


def test_follow_timeout_is_explicit(durable_store) -> None:
    """Run qui ne se règle pas : clôture BORNÉE avec ``follow_timed_out: true``."""
    durable_store.transition("run-1", "running")
    _append(durable_store, "orchestrate.start")

    parsed = _parse(
        _chunks(
            mcp_server_sse._replay_durable_events(
                _replay_payload(run_id="run-1", follow=True)
            )
        )
    )
    completed = next(data for event, data, _ in parsed if event == "replay_completed")
    assert completed["follow_timed_out"] is True
    # L'historique émis reste acquis : la prochaine reconnexion part du
    # curseur retourné (aucune perte, aucun doublon).
    assert completed["last_sequence"] == 1


def test_follow_ignores_events_below_watermark(durable_store) -> None:
    """Reprise d'un run ABOUTI : le backlog complet, mais JAMAIS un doublon."""
    _append(durable_store, "orchestrate.start")
    _append(durable_store, "orchestrate.done")
    durable_store.transition("run-1", "running")
    durable_store.transition("run-1", "completed", checkpoint="completed")

    parsed = _parse(
        _chunks(
            mcp_server_sse._replay_durable_events(
                _replay_payload(run_id="run-1", after_sequence=1, follow=True)
            )
        )
    )
    replays = [data["event"] for event, data, _ in parsed if event == "orchestrate.replay"]
    assert replays == ["orchestrate.done"]  # seq 1 déjà vue : jamais ré-émise
    completed = next(data for event, data, _ in parsed if event == "replay_completed")
    assert completed["last_sequence"] == 2


def test_unknown_run_is_explicit(durable_store) -> None:
    """Run inconnu : erreur explicite (pas un replay vide silencieux)."""
    chunks = _chunks(
        mcp_server_sse._replay_durable_events(_replay_payload(run_id="ghost"))
    )
    parsed = _parse(chunks)
    assert parsed[0][0] == "replay.error"
    assert parsed[0][1]["error_code"] == "run_not_found"
    assert chunks[-1] == "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# 5. Curseur de reprise : contrats crypto (unitaires)
# ---------------------------------------------------------------------------


def test_resume_token_roundtrip() -> None:
    token = resume_cursor.encode_resume_token("run-abc", 42)
    assert resume_cursor.decode_resume_token(token) == ("run-abc", 42)


def test_resume_token_rejects_empty_run_id() -> None:
    with pytest.raises(ValueError, match="run_id must not be empty"):
        resume_cursor.encode_resume_token("  ", 0)


def test_resume_token_rejects_unknown_version() -> None:
    token = resume_cursor.encode_resume_token("run-1", 1)
    body, signature = token.split(".")
    import base64 as _b64

    payload = json.loads(_b64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["v"] = 99
    forged_body = _b64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=").decode()
    with pytest.raises(resume_cursor.ResumeTokenError):
        resume_cursor.decode_resume_token(f"{forged_body}.{signature}")


def test_resume_token_ttl_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("MCP_RESUME_TOKEN_TTL_SECONDS", "1")
    token = resume_cursor.encode_resume_token("run-1", 1, issued_at=time.time() - 5)
    with pytest.raises(resume_cursor.ResumeTokenError) as excinfo:
        resume_cursor.decode_resume_token(token)
    assert excinfo.value.expired is True
    # TTL sain : le même token tout juste émis reste valide.
    fresh = resume_cursor.encode_resume_token("run-1", 1)
    assert resume_cursor.decode_resume_token(fresh) == ("run-1", 1)




