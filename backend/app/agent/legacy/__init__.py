"""Runtime agentique v1 (compatibilité legacy) — DÉPRÉCIÉ.

Ce paquet héberge le runtime agentique historique (``agent_core``,
``orchestrator``, ``runner``, FSM multi-agents, plans, approbations…),
absorbé depuis ``ia/agent`` sans réécriture (strangler).

CONTRAT de migration (S — réduction de la compatibilité legacy) :

    - AUCUN nouveau code ne doit importer directement ``app.agent.legacy.*`` —
      verrouillé par ``backend/tests/test_no_direct_legacy_imports.py`` ;
    - le seul point d'entrée production autorisé est la façade strangler
      ``app.application.agent_cache``, qui résout les symboles PAresseusement
      (``__getattr__`` de module, PEP 562) au premier usage ;
    - chemins de sortie : noyau v2 ``app.agent.core`` / ``app.agent.factory``
      (mono-agent), use-cases ``app.application.*``, ports ``app.domain.ports`` ;
    - feuille de route : supprimer ce paquet une fois l'orchestration
      multi-agents v1 migrée — les tests ciblant encore le v1
      (``tests/test_multi_*.py``, ``tests/test_plan_*.py``, …) seront migrés
      ou retirés avec lui.
"""
