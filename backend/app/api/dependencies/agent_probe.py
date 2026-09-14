# project/app/api/dependencies/agent_probe.py

"""Sonde de connectivité d'un provider LLM — helper HTTP partagé (B-5).

La DÉCISION (URL de sonde, en-têtes, messages) appartient au use case
``app.application.agent_surface.connectivity_plan`` ; ce module exécute
l'appel HTTP et normalise le résultat en ``{"ok", "detail"}`` — contrat 200
unique du dashboard (bouton « Tester »), partagé par la surface legacy et sa
version v1 sans duplication (ADR-0003 §1).

L'audit (``audit_log``) est injecté : c'est le seam de test du module de
wiring ``app.api.routes.agent``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import requests

from app.application import agent_surface
from app.infrastructure.persistence.audit_store import ACT_CONNECT

# Timeout (secondes) des sondes de connectivité du bouton « Tester ».
CONNECTIVITY_TIMEOUT_SECONDS = 8.0


def run_connectivity_probe(
    request: Any,
    *,
    get_config: Callable[[], Any],
    audit_log: Callable[..., Any],
) -> dict:
    """Sonde le provider demandé et renvoie ``{"ok": bool, "detail": str}``.

    Aucune exception n'est levée : le résultat d'échec est un corps 200 que
    l'UI affiche tel quel. ``request`` est le DTO ``ConnectivityTestRequest``
    (champs absents = valeurs effectives de la config courante).
    """
    cfg = get_config()
    provider = (request.provider or "").strip().lower() or str(cfg.provider)
    plan = agent_surface.connectivity_plan(
        provider, fields=request.model_dump(exclude_none=True), cfg=cfg
    )

    try:
        response = requests.get(
            plan.probe_url, headers=plan.headers, timeout=CONNECTIVITY_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        response.json()
    except requests.exceptions.Timeout:
        outcome = {
            "ok": False,
            "detail": f"Délai dépassé ({CONNECTIVITY_TIMEOUT_SECONDS:.0f}s) sur {plan.probe_url}.",
        }
    except requests.exceptions.ConnectionError:
        outcome = {"ok": False, "detail": f"Injoignable : {plan.probe_url}.{plan.hint}"}
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        extra = " Clé API invalide ?" if status == 401 else plan.hint
        outcome = {"ok": False, "detail": f"HTTP {status} sur {plan.probe_url}.{extra}"}
    except ValueError:
        outcome = {"ok": False, "detail": f"Réponse illisible de {plan.probe_url}."}
    else:
        outcome = {"ok": True, "detail": plan.success_detail}

    audit_log(
        ACT_CONNECT,
        subject=provider,
        detail={"provider": provider, "probe_url": plan.probe_url, "ok": outcome["ok"]},
    )
    return outcome
