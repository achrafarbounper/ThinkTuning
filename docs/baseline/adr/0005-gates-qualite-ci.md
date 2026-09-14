# ADR-0005 — Gates qualité en CI (typage, couverture, lint des tests)

**Statut :** Accepté (14/09/2026) · **Décideurs :** équipe SCRUM-141

## Contexte

La CI backend actuelle : `ruff check app/` + `ruff format --check app/` +
`mypy app/ || true` + pytest. Baseline mesurée : mypy non bloquant (1 erreur
résiduelle), `app/api` exclu du scope mypy, aucune couverture (pytest-cov
absent des dev-deps), tests non lintés (76 erreurs ruff dans `tests/`).

## Décision

1. **Séquence de durcissement** (alignée sur le backlog B-7/B-8/B-9) :
   1. `pytest-cov` ajouté aux dev-deps ; step CI
      `pytest --cov=app --cov-fail-under=76` (plancher = baseline).
   2. Correction de l'erreur mypy résiduelle, puis retrait de `|| true` :
      `mypy app/` devient bloquant.
   3. `ruff check app tests` (les 47 erreurs auto-fixables d'abord), puis
      extension du format-check à `tests/`.
2. **Ratchet** : le plancher de couverture ne peut que monter (+1 pt/sprint
   visé) ; toute baisse bloque la PR.
3. Le périmètre mypy reste `app/` hors `app/api` jusqu'à l'épuration du
   handler agentique (B-5) ; l'extension à `app/api` est un jalon explicite,
   pas un défaut silencieux.

## Alternatives rejetées

- Passer mypy bloquant immédiatement sur `app/api` : ~1 910 lignes non typées
  à traiter d'abord (B-5).
- Imposer 90 % de couverture d'emblée : la baseline 76 % sert de plancher
  honnête, le ratchet progresse sans décourager.

## Conséquences

- La dette ne peut plus croître sans signal ; la baseline reste comparable.
- Coût CI : ~+3 min (coverage). Accepté (timeout backend 30 min, marge large).
