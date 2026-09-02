import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist', // Assurez-vous que c'est bien 'dist'
    // Désactive les sourcemaps en production : fichiers .map plus générés,
    // build plus léger et moins de travail pour le parseur au chargement.
    sourcemap: false,
    target: 'es2019',
    rollupOptions: {
      output: {
        // Découpage manuel des bundles — objectif : sortir React du monolithique
        // (~2,7 MB) et ne PAS précharger recharts au démarrage.
        //  - react / react-dom isolés dans react-vendor (~56 KB gzip), le seul
        //    chunk vendor préchargé au premier rendu.
        //  - recharts/d3 restent dans leurs chunks naturels (par page) : chargés
        //    à la demande, jamais dans le chemin critique.
        // ATTENTION : ne pas ajouter de chunk nommé pour recharts ni de
        // catch-all "vendor" — Rollup les attribuerait au graphe de l'entrée et
        // les "modulepreload"-rait au démarrage (~+119 KB gzip inutiles).
        manualChunks(id: string) {
          // On n'isole QUE React (le très gros gain : le monolithique de ~2,7 MB
          // se réduit à ~56 KB gzip). Recharts et les autres node_modules restent
          // dans leurs chunks naturels (par page lazy), donc ils ne sont PAS
          // préchargés au démarrage : ils ne sortent qu'à l'ouverture d'une page
          // de graphiques.
          if (!id.includes('node_modules')) return undefined
          if (
            id.includes('/react/') ||
            id.includes('/react-dom/') ||
            id.includes('/react/jsx-') ||
            id.includes('/scheduler/') ||
            id.includes('react-dom')
          ) {
            return 'react-vendor'
          }
          return undefined
        },
      },
    },
  },
  server: {
    // En développement, /api/* est transféré vers l'API FastAPI locale,
    // ce qui rend fetch("/api/ai") homogène entre dev et production.
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
