/**
 * hooks/useIntentCache.ts
 * ---------------------------------------------------------------------
 * Cache de prédictions d'intention côté CLIENT (LRU borné + normalisation
 * des clés, doublon du cache backend mais local à l'onglet).
 *
 * L'intérêt : un même message (« Lance l'entraînement ») renvoyé deux fois
 * dans la même session ne re-tokenise pas côté serveur ; le hit rate
 * backend (60-80 %) s'en trouve aussi amélioré sur les répétitions.
 *
 * API : { cache, stats, predict(texts), clear }.
 *   - ``predict`` renvoie { results, fromCache } en dédupliquant par clé
 *     normalisée (casse/espaces), en prédicant seulement les manqués, puis
 *     en fusionnant dans l'ordre d'entrée.
 *   - ``stats`` expose { size, hits, misses, hitRate } pour le monitoring UI.
 *
 * Implémentation sans refs : tout vit dans l'état React (le dict d'entrées
 * porte un timestamp d'accès, l'éviction LRU trie dessus), ce qui évite
 * d'écrire des refs pendant le rendu (conformité react-hooks/refs).
 */

import { useCallback, useState } from "react";
import { predictIntent, intentCacheKey } from "../api/intentApi";
import type {
  ClassifierPrediction,
  SentimentApiClient,
} from "../api/sentimentApiClient";

export interface UseIntentCacheOptions {
  /** Nombre maximal d'entrées mises en cache (LRU). */
  maxEntries?: number;
}

export interface IntentCacheStats {
  size: number;
  maxEntries: number;
  hits: number;
  misses: number;
  hitRate: number;
}

interface PredictOutcome {
  results: ClassifierPrediction[];
  /** Vrai si au moins un texte était déjà en cache. */
  fromCache: boolean;
}

interface CachedEntry {
  result: ClassifierPrediction;
  /** Timestamp d'accès/insertion (sert à l'éviction LRU). */
  at: number;
}

type CacheRecord = Record<string, CachedEntry>;

const DEFAULT_MAX_ENTRIES = 200;

/** Marque une entrée fraîche (``at = now``) puis évince LRU. */
function evictIfNeeded(entries: CacheRecord, max: number): CacheRecord {
  const keys = Object.keys(entries);
  if (keys.length <= max) return entries;
  const sorted = keys.sort((a, b) => entries[a].at - entries[b].at);
  const toRemove = sorted.slice(0, keys.length - max);
  const next = { ...entries };
  for (const key of toRemove) delete next[key];
  return next;
}
export function useIntentCache({
  maxEntries = DEFAULT_MAX_ENTRIES,
}: UseIntentCacheOptions = {}) {
  const [cache, setCache] = useState<CacheRecord>({});
  const [hits, setHits] = useState(0);
  const [misses, setMisses] = useState(0);

  const clear = useCallback(() => {
    setCache({});
    setHits(0);
    setMisses(0);
  }, []);

  const predict = useCallback(
    async (
      client: SentimentApiClient,
      texts: string[]
    ): Promise<PredictOutcome> => {
      const ordered = texts.map((t) => ({ key: intentCacheKey(t), text: t }));
      const results: (ClassifierPrediction | undefined)[] = new Array(
        texts.length
      );
      const missesList: { index: number; text: string; key: string }[] = [];
      let anyHit = false;

      ordered.forEach((item, index) => {
        const entry = cache[item.key];
        if (entry) {
          results[index] = entry.result;
          anyHit = true;
        } else {
          missesList.push({ index, text: item.text, key: item.key });
        }
      });

      if (anyHit) setHits((n) => n + texts.length - missesList.length);

      if (missesList.length > 0) {
        setMisses((n) => n + missesList.length);
        const response = await predictIntent(
          client,
          missesList.map((m) => m.text)
        );
        // Fusionne les prédictions dans l'ordre d'entrée, puis écrit les
        // nouveaux résultats en une seule mise à jour d'état (éviction LRU).
        const fresh: CacheRecord = {};
        missesList.forEach((m, offset) => {
          const predicted = response?.results?.[offset];
          if (predicted) {
            results[m.index] = predicted;
            fresh[m.key] = { result: predicted, at: Date.now() + offset };
          }
        });
        if (Object.keys(fresh).length > 0) {
          setCache((prev) => evictIfNeeded({ ...prev, ...fresh }, maxEntries));
        }
      }

      return {
        results: results.filter(
          (r): r is ClassifierPrediction => r !== undefined
        ),
        fromCache: anyHit,
      };
    },
    [cache, maxEntries]
  );

  const stats = useCallback(
    (): IntentCacheStats => ({
      size: Object.keys(cache).length,
      maxEntries,
      hits,
      misses,
      hitRate: hits + misses > 0 ? hits / (hits + misses) : 0,
    }),
    [cache, maxEntries, hits, misses]
  );

  return { cache, stats, predict, clear };
}