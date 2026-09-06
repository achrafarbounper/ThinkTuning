# project/api/schemas/__init__.py
"""Schémas Pydantic des endpoints v1 (contrats HTTP de l'API versionnée).

Les schémas v1 sont la source de vérité du contrat HTTP : ils seront
exportés vers l'OpenAPI (déjà généré par FastAPI) puis consommés par le
dashboard via un client typé — la FIN du couplage implicite frontend/backend.
"""
