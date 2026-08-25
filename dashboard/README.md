# React + Vite

This template provides a minimal setup to get React working in Vite with HMR and some ESLint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Oxc](https://oxc.rs)
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the ESLint configuration

If you are developing a production application, we recommend using TypeScript with type-aware lint rules enabled. Check out the [TS template](https://github.com/vitejs/vite/tree/main/packages/create-vite/template-react-ts) for information on how to integrate TypeScript and [`typescript-eslint`](https://typescript-eslint.io) in your project.

## 💬 Interface de chat (Assistant IA)

Interface de chat façon GitHub Copilot, en **React + TypeScript**, située dans `src/components/chat/` :

```
src/components/chat/
├── ChatWindow.tsx     # État global + appel POST /api/ai en streaming (SSE)
├── ChatMessage.tsx    # Une bulle de message (utilisateur / IA)
├── ChatInput.tsx      # Textarea auto-extensible + bouton envoyer / stop
├── streamSse.ts       # Parseur de flux Server-Sent Events
├── types.ts           # Types partagés
├── chat.css           # Style Copilot / VS Code (clair + sombre)
└── index.ts           # Exports publics
```

Fonctionnalités : streaming token par token, spinner de chargement,
curseur clignotant, scroll automatique intelligent, bouton « Stop »
(AbortController), gestion des erreurs, thème clair/sombre automatique.

### Authentification (X-API-Key)

`POST /api/ai` est protégé par `require_api_key` côté backend : le chat envoie
l'en-tête `X-API-Key` à chaque requête. La clé est résolue dans cet ordre :

1. la configuration persistée par le dashboard (`localStorage`, clé
   `thinktuning.apiConfig` — champ « API_KEY côté serveur » du formulaire
   Configuration) ;
2. la variable d'environnement Vite `VITE_API_KEY` (fichier
   `dashboard/.env.local`, ex. `VITE_API_KEY=dev-local-api-key`).

Sans clé, la requête part sans en-tête et le backend répond 401 ; le message
d'erreur s'affiche alors dans la bulle du chat.

### Lancer le tout en développement

```bash
# Terminal 1 — backend (au choix) :
venv\Scripts\python scripts\mock_ai_backend.py        # mini-serveur de test autonome
# ou la vraie API : uvicorn api.main:app --port 8000 --reload

# Terminal 2 — frontend :
cd dashboard && npm run dev                            # http://localhost:5173
```

Le proxy Vite transfère `/api/*` vers `http://localhost:8000`
(voir `vite.config.js`), donc `fetch("/api/ai")` fonctionne tel quel.

### Scripts utiles

| Commande             | Rôle                                  |
| -------------------- | ------------------------------------- |
| `npm run dev`        | Serveur de développement Vite         |
| `npm run typecheck`  | Vérification TypeScript (`tsc`)       |
| `npm run lint`       | ESLint (JS **et** TS/TSX)             |
| `npm run build`      | Build de production dans `dist/`      |

### Brancher votre vrai modèle IA

Remplacez `_build_reply()` dans `api/routes/ai_chat.py` par l'appel à votre
modèle. Le contrat est simple : émettre des événements SSE
`data: {"delta": "fragment"}` puis `data: [DONE]`. Le frontend gère aussi un
repli JSON non streamé (`{"content": "..."}`).

