"""Cas d'usage (application layer) — orchestration sans logique HTTP.

Règle d'or : ``app/application/**`` ne dépend que de ``app/domain`` (et, le
temps de la migration, des adaptateurs injectés par la couche ``api/``).
Aucun import FastAPI, aucune mapping HTTP : les routes restent des adaptateurs
minces qui traduisent les résultats / erreurs des use-cases.
"""
