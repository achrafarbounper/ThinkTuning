# Recommandations d'architecture — ThinkTuning

> Document issu de l'audit complet (backend Python / frontend React-TS).
> Complète `ARCHITECTURE.md` : ici, on se concentre sur **pourquoi** les bugs
> se sont produits et **comment structurer** pour les éviter durablement.

---

## 1. Causes racines des bugs corrigés (à ne pas reproduire)

| Cause racine | Bugs engendrés | Règle d'or |
|---|---|---|
| **État dupliqué** | `_job_cancel_events` défini 5× (routes + runners) ; `API_KEY` défini 2× ; re-export par valeur de `_predictor` (référence obsolète après reload) | Une seule source de vérité par concept. Les routes **délèguent** au domaine, elles ne possèdent pas d'état. |
| **Verrou trop large** | `predictor_cache` tenait le lock pendant le chargement torch → API entière bloquée | Ne verrouiller que la **mutation de la structure**, jamais l'opération lente (I/O, chargement). |
| **Entrée non bornée** | `/predict` acceptait `[]` (crash 500) et des lots illimités (DoS/OOM) | Toute entrée : validation Pydantic **bornée** (min/max) + garde-fou taille fichier + chunking des traitements lourds. |
| **Transport contourné** | `getMetricsRaw/Json` en `fetch` brut : sans timeout, sans clé API, sans normalisation d'erreurs | 100 % des appels passent par la couche transport (`_send`). Aucun `fetch` direct dans les composants/pages. |
| **Fusion de valeurs avec `\|\|`** | impossible d'effacer `openrouterApiKey` (chaîne vide remplacée par l'ancienne) | Distinguer `undefined` (absent) de `""` (effacement explicite). Fusionner via updater fonctionnel. |
| **Test qui écrit dans l'état réel** | `test_trainer.py` publiait des stubs dans `experiments/models/` → `/predict` cassé ensuite pour tout le monde | Tout test redirige `MODEL_ROOT`, `JOB_STORE_PATH`, bases SQLite vers `tmp_path`. Un test n'écrit jamais hors de son bac à sable. |
| **Erreurs avalées** | `except Exception: raise HTTPException(400)` sans cause | `raise … from exc` + log. Un catch large doit toujours préserver la trace. |
| **Secret codé en dur** | `openrouter_api_key` réel (`sk-or-v1-…`) posé en défaut dans `Settings` → secret exposé dans le source ; masquait aussi les tests de validation | Aucune valeur sensible par défaut : clé = `None`, lue uniquement via l'environnement. **Rotation de toute clé ayant transité par un dépôt.** |
| **Worker lancé 2×** | le test e2e démarrait un 2d thread `run_training` pendant que `POST /train` en lançait déjà un → double entraînement du même `job_id`, collisions sur `<version>.tmp/` → `PermissionError`/`FileNotFoundError` **intermittents sous charge (Windows)** | Un `POST` = un worker. Le test qui « vérifie le polling » ne relance jamais le worker lui-même. |
| **Dérive défauts ↔ tests** | `Settings` passé à `OPENROUTER` + 9 flags, mais `test_defaults` attendait `OLLAMA` + 7 flags → suite rouge sans lien avec le code livré | Les tests de défauts doivent être **déterministes** (`get_settings(env_file=None)`, env nettoyé par fixture) et refléter les défauts commités, pas l'état d'un `.env` local. |

---

## 2. Architecture cible (backend)

La migration hexagonale (`app/` : ports + adapters, noyau v2 agentique) est la
bonne direction. Cible à consolider :

```
thinktuning/
├── api/                        # Couche HTTP — MINCE (routes, middlewares, schémas)
│   ├── main.py                 # Câblage app (aucune logique métier)
│   ├── dependencies/           # auth, DI
│   ├── middlewares/            # maintenance, rate_limit, metrics
│   └── routes/                 # 1 fichier par ressource, délégation pure
├── core/                       # Services applicatifs (job_store, runners, caches, stores)
├── app/                        # Noyau hexagonal v2 (domain, application, infrastructure, ports)
├── src/                        # ML : dataset, model, inference (Predictor)
├── ia/                         # Agent IA : outils, prompt, registre (→ à absorber dans app/ à terme)
├── scripts/                    # CLI opérationnels (dump_tool_manifest, mock_ai_backend…)
├── tests/                      # pytest, 1 fichier par module, sandbox tmp_path obligatoire
└── configs/, data/, docs/
```

Règles d'interface :

1. **Flux unique** : `route → service (core/ ou app/application) → infrastructure`.
   Une route ne touche jamais directement SQLite, torch ou un fichier.
2. **Seams de test explicites** : les tests monkeypatchent `api._get_predictor`
   (fonction) — jamais des variables privées. Si un test a besoin d'un état,
   exposer une fonction, pas une variable.
3. **Annulation** : tout runner long expose `get_cancel_event(job_id)` ;
   la route l'appelle, elle ne crée pas son propre `Event`.
4. **Configuration** : continuer la migration vers `app/config/settings.py`
   (`get_settings()`), seule lecture d'environnement autorisée.
   ⚠️ Le fail-fast provider LLM actuel doit devenir *lazy* (validé à la
   première utilisation de l'agent), sinon l'API ne démarre pas sans clé
   OpenRouter alors qu'elle peut servir `/predict`.
5. **WebSockets/SSE** : publier via `EventBusPort` (déjà câblé) ; interdire
   tout polling interne caché côté route.

## 3. Architecture cible (frontend)

```
dashboard/src/
├── api/            # Transport (clientCore) + endpoints métier (1 module/domaine)
├── context/        # AppProvider : config, santé, modèles, logs (état GLOBAL uniquement)
├── hooks/          # usePolling, useLocalStorage, useExplain… (réutilisables, testés)
├── components/     # ui/ (primitifs), chat/, flowmap/ (domaines)
├── pages/          # Composition uniquement — pas de logique réseau
├── lib/            # format, sentiment (helpers purs)
└── test/           # setup vitest + doubles
```

Règles :

1. **Un seul transport** : composants/pages → hooks/services → `api/` →
   `clientCore._send`. Aucun `fetch` ailleurs.
2. **État global minimal** : seules les données *partagées entre pages*
   (config, santé, modèles, historique) vivent dans le contexte. Le reste
   reste local à la page ou dans un hook dédié — c'est ce qui évite les
   re-renders en cascade.
3. **Persistance** : toujours via `useLocalStorage` (jamais de
   `localStorage.setItem` manuel dans un composant — c'est cette duplication
   qui a rendu impossible l'effacement de la clé OpenRouter).
4. **Parsing réseau** : les flux SSE sont consommés via `streamSse.ts`
   (conforme à la spec : blocs, data multiligne, une seule espace de tête).
   Tout nouveau format de flux → un module dédié + tests.
5. **Clés de liste** : jamais d'index seul quand la liste peut être réordonnée
   (insertion en tête de l'historique = bug de réconciliation typique).

## 4. Hygiène de tests

- **Sandbox obligatoire** : `MODEL_ROOT`, `JOB_STORE_PATH`, `AGENT_*_PATH`
  redirigés vers `tmp_path`. Une fixture autouse dans un futur
  `tests/conftest.py` global fermerait la porte à toute nouvelle fuite
  (c'est celle de `test_trainer.py` qui a publié 24 stubs dans l'état réel).
- **Un seul seam par dépendance** : mocker `api._get_predictor` (et non tantôt
  `core.predictor_cache.get_predictor`, tantôt la variable `_predictor`).
- **Frontend** : `npm test` (vitest + RTL, 28 tests). Priorité aux modules
  critiques : transport (`clientCore`), hooks génériques (`usePolling`,
  `useLocalStorage`), parsing (`streamSse`). Les pages sont couvertes par
  `npm run typecheck` + build.

## 5. Performance — acquis et prochaines étapes

| Acquis | Gain |
|---|---|
| Cache LRU prédicteurs (3 versions) + chargement hors verrou | Fin du blocage global ; alternance de modèles sans rechargement complet |
| Inférence : `PREDICT_DEVICE=auto` (CUDA), `torch.inference_mode`, chunks `PREDICT_CHUNK_SIZE` | ×2-10 sur GPU, OOM impossible sur les gros lots |
| `/predict/batch` : bornes fichier/lignes + chunking d'inférence | Latence et mémoire bornées, ordre des résultats préservé |
| Lifespan : sanity check modèle en thread daemon | Démarrage de l'API non bloqué par le chargement du modèle |
| Rate limit : purge des buckets inactifs | Plus de croissance mémoire illimitée |
| Front : premier poll différé, pause onglet caché, bundle react-vendor | LCP non bloqué par le réseau |

Prochaines pistes (non bloquant) :
- streaming NDJSON de `/predict/batch` pour les très gros CSV ;
- `useSyncExternalStore` si le contexte global devient un goulot de re-renders ;
- purge planifiée des jobs (cron/scheduler) via `cleanup_old_jobs()` désormais
  fonctionnel.

## 6. Checklist anti-régression (avant toute PR)

- [ ] `pytest` complet vert (aucune écriture hors `tmp_path`) ;
- [ ] `npm run typecheck && npm test && npm run build` verts ;
- [ ] nouvelle entrée API → bornes Pydantic + test 422 ;
- [ ] nouvel état partagé → source unique, pas de copie locale ;
- [ ] nouvelle dépendance réseau frontend → passe par `clientCore` ;
- [ ] nouvelle variable d'environnement → documentée dans `.env.example`
      et lue via `app/config/settings.py` (ou wrapper dédié).

