# ThinkTuning Dashboard

Dashboard React autonome du projet ThinkTuning. Le frontend est servi par
Vite en développement et par nginx dans l'image Docker `frontend/`. Il
consomme exclusivement l'API FastAPI versionnée du dossier `backend/`.

## Stack

- React 19
- TypeScript 6
- Vite 8
- Vitest + React Testing Library
- ESLint 10
- Recharts pour les graphiques

## Structure

```text
src/
├── api/          # client HTTP, clients métier et types OpenAPI générés
├── components/   # composants réutilisables, dont components/chat/
├── context/      # état global de l'application
├── hooks/        # hooks réutilisables
├── lib/          # fonctions utilitaires
├── pages/        # composition des écrans
└── test/         # configuration Vitest
```

Le transport centralisé de `src/api/` ajoute l'URL de base, le
`X-API-Key`/jeton de session et l'enveloppe d'erreur v1. Les composants ne
doivent pas appeler `fetch` directement. Les flux SSE passent par
`src/components/chat/streamSse.ts` ou le client spécialisé correspondant.

## Développement local

Depuis la racine du dépôt, démarrer l'API :

```powershell
cd backend
python -m uvicorn app.api.main:app --reload --port 8000
```

Dans un second terminal :

```powershell
cd frontend
npm install
npm run dev
```

Le serveur Vite est disponible sur `http://localhost:5173`. Les proxys
`/api/*` et `/mcp/*` sont transférés vers `http://localhost:8000` par
`vite.config.ts`; aucune URL backend en dur n'est nécessaire dans les appels
du dashboard.

En production, l'image frontend nginx proxifie les mêmes chemins vers le
service API. La clé `API_KEY` est injectée par nginx côté serveur dans
Docker Compose : elle n'est pas stockée dans `localStorage` ni exposée au
bundle JavaScript.

## Commandes

| Commande | Rôle |
| --- | --- |
| `npm run dev` | Serveur Vite avec HMR |
| `npm run typecheck` | Vérification TypeScript |
| `npm run lint` | ESLint |
| `npm run test` | Tests Vitest |
| `npm run build` | Build de production dans `dist/` |
| `npm run preview` | Prévisualisation du build |
| `npm run generate:api-types` | Régénération depuis `../backend/openapi.json` |

## Contrat backend

Les routes applicatives sont préfixées par `/api/v1`. Les routes de lecture
publiques restent accessibles sans clé selon le contrat backend; les
mutations et prédictions protégées utilisent `X-API-Key`. Le transport
frontend attend l'enveloppe d'erreur v1 :

```json
{ "error": { "code": "validation_error", "message": "...", "details": {} } }
```

Pour l'API complète, consulter [`../ARCHITECTURE.md`](../ARCHITECTURE.md) et
[`../docs/ARCHITECTURE_DECOUPLAGE.md`](../docs/ARCHITECTURE_DECOUPLAGE.md).
