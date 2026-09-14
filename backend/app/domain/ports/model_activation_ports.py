# project/app/domain/ports/model_activation_ports.py
"""Port « activation des versions de modèles » (ADR-0003 §3, écart E-03).

Rupture du dernier cycle d'imports (baseline 7 → 0) :
``persistence/model_versioning`` n'importe plus ``application/model_activation`` —
la logique « quelle version est active » est décrite ici comme un CONTRAT du
domaine, l'accès disque (pointeur ``active.json``, catalogue ``MODEL_ROOT``,
contrôle de tête entraînée) est fourni par un adaptateur infrastructure
enregistré via :func:`register_model_activation_port`.

Sens des dépendances après B-3 (unidirectionnel) :

    application/model_activation   ->  domain (ce port)
    infrastructure/model_versioning ->  domain (ce port, adaptateur par défaut)
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ModelActivationPort(Protocol):
    """Contrat « catalogue + pointeur de version active » (SCRUM-55).

    L'adaptateur par défaut vit dans ``persistence/model_versioning`` ; les
    tests peuvent substituer un fake via :func:`register_model_activation_port`.
    Les lectures d'environnement et de chemins se font À CHAQUE appel
    (monkeypatch-friendly, parité avec l'implémentation historique).
    """

    def model_root(self) -> str:
        """Racine du catalogue de versions (lue à chaque appel)."""
        ...

    def list_model_versions(self) -> list[str]:
        """Versions valides (poids non vides), de la plus récente à la plus ancienne."""
        ...

    def is_model_version_trained(self, version_dir: str) -> bool:
        """True si la tête de classification atteste un entraînement réel (std > 0.03)."""
        ...

    def get_active_pointer_path(self) -> str:
        """Chemin du pointeur actif (env ``ACTIVE_MODEL_POINTER`` lue à chaque appel)."""
        ...

    def read_active_pointer(self) -> dict[str, Any] | None:
        """Dict du pointeur actif, ou None si absent/corrompu."""
        ...

    def write_active_pointer(self, version: str, path: str, f1_macro: float | None = None) -> dict:
        """Écriture atomique (tmp + rename) du pointeur actif ; retourne le dict écrit."""
        ...

    def get_active_model_dir(self) -> str | None:
        """Chemin du dossier de la version active, ou None si aucune/invalide."""
        ...


_adapter: ModelActivationPort | None = None


def register_model_activation_port(adapter: ModelActivationPort) -> None:
    """Enregistre (ou remplace) l'implémentation du port (idempotent)."""
    global _adapter
    _adapter = adapter


def reset_model_activation_port() -> None:
    """Retire l'implémentation enregistrée (isolation des tests)."""
    global _adapter
    _adapter = None


def get_model_activation_port() -> ModelActivationPort:
    """Implémentation enregistrée (RuntimeError explicite si aucune).

    L'adaptateur par défaut s'enregistre au chargement de
    ``app.infrastructure.persistence.model_versioning`` : tout flux qui lit le
    catalogue de versions l'a donc déjà importé (parité avec l'ancien
    lazy-import, qui supposait le même module chargé).
    """
    if _adapter is None:
        raise RuntimeError(
            "ModelActivationPort non enregistré : importer "
            "``app.infrastructure.persistence.model_versioning`` (adaptateur "
            "par défaut, ADR-0003 §3) ou enregistrer un fake via "
            "``register_model_activation_port``."
        )
    return _adapter


def resolve_active_model_dir() -> str | None:
    """Version active via le port, ou ``None`` si port absent (repli appelant).

    Sémantique identique à l'ancien lazy-import défensif de
    ``model_versioning.resolve_model_dir`` : l'absence d'implémentation ne doit
    JAMAIS casser la résolution (repli sur la dernière version valide).
    """
    if _adapter is None:
        return None
    return _adapter.get_active_model_dir()
