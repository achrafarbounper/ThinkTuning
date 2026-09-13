"""Cas d'usage (application layer) — orchestration sans logique HTTP.

Règle d'or : ``app/application/**`` ne dépend que de ``app/domain`` (et, le
temps de la migration, des adaptateurs injectés par la couche ``api/``).
Aucun import FastAPI, aucune mapping HTTP : les routes restent des adaptateurs
minces qui traduisent les résultats / erreurs des use-cases.

Services réabsorbés de ``app/legacy/core`` (paquet supprimé) : runners
d'entraînement (``trainer_runner``, ``intent_trainer``), caches et registres
ML (``predictor_cache``, ``prediction_result_cache``, ``classifier_registry``),
services d'agent (``agent_cache``, ``feature_flags``,
``scheduler``, ``training_gate``) et orchestrateurs pipeline / cycle
active learning. Dette transitoire assumée : ``agent_cache`` et
``predictor_cache`` lèvent encore ``fastapi.HTTPException`` (contrat v1
conservé) — à remplacer par des erreurs de domaine lors de la strangulation.
"""
