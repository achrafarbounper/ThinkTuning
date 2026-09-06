/**
 * lib/sentiment.ts
 * ---------------------------------------------------------------------
 * Libellés et utilitaires métier liés aux sentiments, partagés par toutes
 * les pages du dashboard (évite la duplication constatée dans HomePage,
 * SentimentPage, ComparePage, DriftPage et EvaluationPage).
 */

/** Libellé français de chaque sentiment renvoyé par l'API. */
export const SENTIMENT_LABELS: Record<string, string> = {
  negative: "négatif",
  neutral: "neutre",
  positive: "positif",
};

/** Libellé français avec majuscule (usage en tête de section / titre). */
export const SENTIMENT_LABELS_FR: Record<string, string> = {
  negative: "Négatif",
  neutral: "Neutre",
  positive: "Positif",
};

/**
 * Traduit un sentiment en libellé français. Tout sentiment inconnu ou absent
 * est restitué tel quel (ou « — » si absent/nul), au lieu d'afficher undefined.
 */
export function sentimentLabel(sentiment?: string | null): string {
  if (!sentiment) return "—";
  return SENTIMENT_LABELS[sentiment] ?? sentiment;
}

/** Version capitalisée (titre de section). */
export function sentimentLabelCapitalized(label?: string | null): string {
  if (!label) return "—";
  return SENTIMENT_LABELS_FR[label] ?? label;
}

/** Ordre d'affichage canonique des classes de sentiment. */
export const SENTIMENT_ORDER = ["negative", "neutral", "positive"] as const;

export type SentimentClass = (typeof SENTIMENT_ORDER)[number];