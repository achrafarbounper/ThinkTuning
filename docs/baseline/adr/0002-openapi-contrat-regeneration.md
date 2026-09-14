# ADR-0002 — Régénération du contrat OpenAPI et épinglage des versions

**Statut :** Accepté (14/09/2026) · **Décideurs :** équipe SCRUM-141

## Contexte

Deux tests de fraîcheur du contrat échouent : l'`openapi.json` commité diffère
du spec reconstruit. La différence porte sur la sérialisation JSON Schema
(`"format": "binary"` vs `"contentMediaType": "application/octet-stream"`).
Cause racine : `fastapi>=0.110.0` / `pydantic>=2.0.0` flottants — l'environnement
résout fastapi 0.141.1 / pydantic 2.13.5, dont la génération a changé depuis la
génération du fichier commité. Constat connexe : le venv local a starlette 1.0.1
alors que les pins exigent `==1.3.1`.

## Décision

1. **Rejouer la chaîne de contrat dans l'ordre** en un seul changement :
   `python export_openapi.py` → `openapi.json` → `npm run generate:api-types`
   (`schema.d.ts`) → commit atomique + tests de fraîcheur verts.
2. **Épingler les versions de sérialisation** (`fastapi`, `pydantic`,
   `starlette`) dans `requirements.txt` et `pyproject.toml` : la génération du
   spec ne doit pas dépendre de la date d'installation.
3. Le verrou croisé backend↔client (`test_api_v1_client_contract.py`) reste la
   référence sémantique ; les tests de fraîcheur restent exacts (égalité JSON).

## Alternatives rejetées

- Assouplir les tests de fraîcheur (comparer les paths uniquement) : perdrait la
  détection des dérives de DTO.
- Épingler uniquement starlette : la sérialisation dépend de fastapi+pydantic.

## Conséquences

- Le client TS généré redevient une source de vérité fiable.
- La baseline peut être utilisée pour prouver les régressions de contrat.
