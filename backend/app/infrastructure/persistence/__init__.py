"""Adapters de persistance (SQLite / MongoDB) utilisés par le runtime.

Regroupe les stores réabsorbés de ``app/legacy/core`` (paquet supprimé) :
stores session / audit / run / approval / flow / intent / annotation / job /
mcp_client, versionnage et vérification des modèles (``model_versioning``,
``model_head_check``), chiffrement au repos (``store_crypto``), masquage de
secrets (``secrets_redact``), paramètres persistés de l'agent
(``agent_settings``) et source d'événements d'entraînement
(``training_events``).

S'y ajoutent les adaptateurs MongoDB (``mongodb.py``, backend
``PERSISTENCE_BACKEND=mongodb``), le module neutre partagé ``common.py``
(provider client Mongo + registre late-binding des stores, ADR-0004) et
l'alias de compatibilité ``sqlite.py``.
"""
