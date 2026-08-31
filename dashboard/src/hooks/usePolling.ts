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
  /** Fonction appelée à chaque tick. Doit être stable. */
  tick: () => void | Promise<void>;
}

export function usePolling({
  intervalMs,
  immediate = true,
  pauseWhenHidden = true,
  enabled = true,
  tick,
}: UsePollingOptions): void {
  useEffect(() => {
    if (!enabled) return;

    let active = true;
    const run = () => {
      if (active) void tick();
    };

    if (immediate) run();

    const id = window.setInterval(() => {
      if (pauseWhenHidden && document.hidden) return;
      run();
    }, intervalMs);

    const onVisibilityChange = () => {
      // Retour sur l'onglet : on rafraîchit immédiatement au lieu d'attendre
      // le prochain interval (données potentiellement périmées).
      if (!document.hidden && active) run();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      active = false;
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [intervalMs, immediate, pauseWhenHidden, enabled, tick]);
}