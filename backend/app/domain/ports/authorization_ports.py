"""Ports d'autorisation (P2 Lot A) — contrats PDP versionnés.

``PolicyDecisionPoint`` est le port de DÉCISION : il répond « ce sujet a-t-il
le droit d'exécuter cette action sur cette ressource ? » de façon pure,
déterministe et rapide (p95 < 5 ms local — aucune I/O réseau dans le chemin
de décision). Les adaptateurs possibles :

    - Casbin embarqué (défaut Lot A) : ``app/infrastructure/security/authz`` ;
    - PDP distante (OPA/Rego) : un adaptateur qui encapsule l'appel HTTP et
      qui DOIT fail-closer (déni) en cas d'indisponibilité.

Le port de SOURCE (``PolicySourcePort``) est séparé : charger / recharger le
document de politique (fichier JSON aujourd'hui, Valkey demain) est une
préoccupation distincte de la décision, et le cache Valkey n'est JAMAIS la
source de vérité (cf. décisions du plan P2).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain.authorization import AuthzDecision, AuthzRequest


@runtime_checkable
class PolicyDecisionPoint(Protocol):
    """Contrat du point de décision de politique (PDP).

    L'implémentation DOIT :
        - être déterministe et locale (aucune I/O réseau par décision) ;
        - refuser (deny) toute requête qu'elle ne sait pas évaluer (fail-closed) ;
        - porter ``policy_version`` dans chaque décision (traçabilité / rollback).
    """

    def authorize(self, request: AuthzRequest) -> AuthzDecision:
        """Décide une requête (jamais d'exception métier : fail-closed en deny)."""
        ...

    @property
    def policy_version(self) -> int:
        """Version de la politique chargée (audit + compatibilité client)."""
        ...


@runtime_checkable
class PolicySourcePort(Protocol):
    """Contrat de la source du document de politique (fichier / store / cache).

    La source retourne un document VALIDÉ ; elle ne décide rien. Valkey peut
    servir de cache de lecture mais jamais de source de vérité : en cas de
    divergence ou d'indisponibilité, c'est la source locale qui prime.
    """

    def load(self) -> dict:
        """Charge le document de politique brut (dict JSON validé ensuite)."""
        ...

    def fingerprint(self) -> str:
        """Empreinte stable du document (détection de dérive / audit)."""
        ...
