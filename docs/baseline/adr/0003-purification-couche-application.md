# ADR-0003 — Purification incrémentale de la couche application (strangler)

**Statut :** Accepté (14/09/2026) · **Décideurs :** équipe SCRUM-141

## Contexte

La règle d'or d'`ARCHITECTURE.md` veut que `application/` ne dépende que du
domaine. Or ~20 modules de `application/` (services réabsorbés de l'ex-`core/`)
importent directement `app.infrastructure.*` (~40 imports) : agent_cache,
trainer_runner, cycle_runner, scheduler, intent_trainer, pipeline_runner,
session_memory, copilot… Inversement, `infrastructure/persistence/model_versioning.py`
importe `application/model_activation` et `application/model_signing`
(inversion de couche). Le domaine, lui, est pur.

## Décision

1. **Pas de big-bang** : purification module par module, à l'occasion des
   évolutions (strangler), en commençant par ceux qui bloquent les tests
   (agent_cache, P0 du déterminisme).
2. Chaque dépendance `application → infrastructure` est remplacée par un **port
   du domaine** + injection (container existant `composition.py`), ou par le
   déplacement du module dans `infrastructure/` quand il est de nature
   technique (ex. `model_signing`, `model_activation` : candidats à
   requalifier en infrastructure/adapters).
3. L'inversion E-03 est traitée par un port `ModelActivationPort` : la logique
   « quelle version est active » appartient au domaine, l'accès disque au
   port ; `model_versioning.py` ne doit plus importer `application/`.
4. Un **test de garde** étend (à partir de `test_no_direct_legacy_imports.py`)
   interdit tout `import app.infrastructure` hors de `infrastructure/` et
   `api/` ; les exceptions déclarées sont temporaires et datées.

## Alternatives rejetées

- Déplacer tous les services vers `infrastructure/` : perdrait la notion de use
  case et transformerait l'hexagonale en couches horizontales classiques.
- Réécrire les use cases v2 en parallèle : duplication de l'état (cause
  racine documentée des bugs passés).

## Conséquences

- Testabilité réelle des use cases (fakes en mémoire contre Protocols).
- Risque maîtrisé : chaque module migré est verrouillé par ses tests existants.
