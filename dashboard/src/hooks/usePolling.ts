/**
 * hooks/usePolling.ts
 * ---------------------------------------------------------------------
 * Polling réutilisable, sécurisé et économe :
 *  - exécution immédiate au montage (optionnelle),
 *  - mise en pause automatique quand l'onglet est masqué (document.hidden),
 *  - re-rafraîchissement immédiat au retour sur l'onglet,
 *  - interruption propre au démontage (pas de setState après unmount).
 *
 * L'appelé doit être stable (useCallback) pour ne pas relancer l'effet.
 */

import { useEffect } from "react";

export interface UsePollingOptions {
  /** Délai entre deux exécutions, en millisecondes. */
  intervalMs: number;
  /** Exécution immédiate au montage (avant le premier interval). */
  immediate?: boolean;
  /** Met en pause quand l'onglet est masqué, sinon continue en arrière-plan. */
  pauseWhenHidden?: boolean;
  /** Suspend le polling manuellement (ex. pas de clé API renseignée). */
  enabled?: boolean;
  /**
   * Diffère le tout premier tick de `initialDelayMs` ms (défaut : 0).
   * Permet de sortir une requête non critique (ex. /health) du chemin
   * critique : le rendu initial et le LCP ne bloquent plus sur le réseau.
   * N'affecte que le premier tick ; ensuite l'intervalle habituel s'applique.
   */
  initialDelayMs?: number;
  /** Fonction appelée à chaque tick. Doit être stable. */
  tick: () => void | Promise<void>;
}

export function usePolling({
  intervalMs,
  immediate = true,
  pauseWhenHidden = true,
  enabled = true,
  initialDelayMs = 0,
  tick,
}: UsePollingOptions): void {
  useEffect(() => {
    if (!enabled) return;

    let active = true;
    let initialTimeoutId: number | undefined;
    let intervalId: number | undefined;

    // Exécute le tick en interceptant les rejets : un tick qui lève sans
    // catch interne produirait sinon un « unhandled rejection » silencieux
    // à chaque intervalle. Les ticks gèrent leurs erreurs métier eux-mêmes ;
    // ce catch n'est qu'un filet de sécurité.
    const run = () => {
      if (!active) return;
      Promise.resolve()
        .then(tick)
        .catch(() => {
          /* erreur déjà journalisée par le tick */
        });
    };

    // Démarre le cycle complet (tick initial optionnel + setInterval).
    const start = () => {
      if (!active) return;
      // Onglet déjà masqué au démarrage : skip du premier tick (il sera
      // déclenché par visibilitychange au retour, données plus fraîches).
      if (immediate && !(pauseWhenHidden && document.hidden)) run();
      intervalId = window.setInterval(() => {
        if (pauseWhenHidden && document.hidden) return;
        run();
      }, intervalMs);
    };

    // Premier tick différé hors du chemin critique, puis intervalle normal.
    if (initialDelayMs > 0) {
      initialTimeoutId = window.setTimeout(start, initialDelayMs);
    } else {
      start();
    }

    const onVisibilityChange = () => {
      // Retour sur l'onglet : on rafraîchit immédiatement au lieu d'attendre
      // le prochain interval (données potentiellement périmées).
      if (!document.hidden && active) run();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      active = false;
      window.clearTimeout(initialTimeoutId);
      window.clearInterval(intervalId);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [intervalMs, immediate, pauseWhenHidden, enabled, initialDelayMs, tick]);
}