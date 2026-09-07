/**
 * intentApi.ts
 * ---------------------------------------------------------------------
 * Façade « intention » (chat / action) par-dessus le client API unique.
 *
 * Découverte : la classification d'intention est exposée par l'endpoint
 * ``POST /classifiers/intent/predict`` (Phase 5). On ne duplique PAS le
 * transport (``clientCore``) : on réutilise les méthodes du client partagé
 * et on aligne ici les types/labels métier spécifiques à l'intention.
 */

import type {
  ClassifierPrediction,
  SentimentApiClient,
} from "./sentimentApiClient";

export type { ClassifierPrediction };

/** Libellés français des classes d'intention (routage chat/action). */
export const INTENT_LABELS_FR: Record<string, string> = {
  action: "action",
  chat: "discussion",
};

/** Libellé français d'une classe d'intention (inconnu → tel quel). */
export function intentLabel(label?: string | null): string {
  if (!label) return "—";
  return INTENT_LABELS_FR[label] ?? label;
}

/** Instantané du classifieur d'intention (info, métriques, health). */
export interface IntentClassifierInfo {
  name?: string;
  info?: {
    engine?: string;
    labels?: string[];
    threshold?: number;
    model_name?: string;
    [key: string]: unknown;
  };
  metrics?: {
    predictions?: number;
    cached_hits?: number;
    cached_misses?: number;
    errors?: number;
    [key: string]: unknown;
  };
  health?: { ok?: boolean; label?: string; [key: string]: unknown };
  warmup?: { ok?: boolean; [key: string]: unknown } | null;
  [key: string]: unknown;
}

/** Prédit l'intention d'une liste de textes (via le classifieur ``intent``). */
export function predictIntent(
  client: SentimentApiClient,
  texts: string[]
): Promise<{ results?: ClassifierPrediction[] } | null> {
  return client.predictClassifier("intent", texts);
}

/** Recharge le modèle du classifieur d'intention. */
export function reloadIntentModel(client: SentimentApiClient) {
  return client.reloadClassifier("intent");
}

/** Récupère l'état/les métriques du classifieur ``intent``. */
export async function getIntentClassifierInfo(
  client: SentimentApiClient
): Promise<IntentClassifierInfo | null> {
  const listed = await client.listClassifiers();
  const item = listed?.classifiers?.find((c) => c?.name === "intent");
  return (item as IntentClassifierInfo | undefined) ?? null;
}

/** Normalise un texte pour servir de clé de cache côté client. */
export function intentCacheKey(text: string): string {
  return text.trim().toLowerCase();
}