/**
 * lib/format.ts
 * ---------------------------------------------------------------------
 * Formateurs purs, partagés par toutes les pages (évite la duplication des
 * `toFixed` / `toLocaleString` disséminés dans les pages du dashboard).
 * Fonctions sans effet de bord : candidates idéales au test unitaire.
 */

/** Pourcentage à 1 décimale à partir d'une fraction (0..1). Ex : 0.835 → "83.5%". */
export function formatPct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

/** Pourcentage en points entiers (0..1 → "84%"). */
export function formatPctRounded(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${Math.round(value * 100)}%`;
}

/** Heure locale compacte HH:MM. */
const timeFormatter = new Intl.DateTimeFormat("fr-FR", {
  hour: "2-digit",
  minute: "2-digit",
});

/** Heure locale précise HH:MM:SS (graphiques en temps réel). */
const timeSecondsFormatter = new Intl.DateTimeFormat("fr-FR", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

/** Date + heure locale lisible (historique). */
const dateTimeFormatter = new Intl.DateTimeFormat("fr-FR", {
  dateStyle: "short",
  timeStyle: "medium",
});

export function formatTime(ts?: number | string | Date): string {
  if (!ts) return "—";
  const date = ts instanceof Date ? ts : new Date(ts);
  if (Number.isNaN(date.getTime())) return "—";
  return timeFormatter.format(date);
}

export function formatTimeSeconds(ts?: number | string | Date): string {
  if (!ts) return "—";
  const date = ts instanceof Date ? ts : new Date(ts);
  if (Number.isNaN(date.getTime())) return "—";
  return timeSecondsFormatter.format(date);
}

export function formatDateTime(ts?: number | string | Date): string {
  if (!ts) return "—";
  const date = ts instanceof Date ? ts : new Date(ts);
  if (Number.isNaN(date.getTime())) return "—";
  return dateTimeFormatter.format(date);
}

/**
 * Durée compacte à partir de millisecondes : « 320 ms », « 1,2 s »,
 * « 3 min ». (Réutilise la logique du chat pour rester cohérent partout.)
 */
export function formatDuration(durationMs: number): string {
  if (durationMs >= 60_000) return `${(durationMs / 60_000).toFixed(1)} min`;
  if (durationMs >= 1_000) return `${(durationMs / 1_000).toFixed(1)} s`;
  return `${Math.round(durationMs)} ms`;
}

/**
 * Taille lisible d'un nombre d'octets : « 1,2 GB », « 850 MB »…
 * Utilitaire prévu pour l'affichage des tailles de modèles.
 */
export function formatBytes(bytes?: number | null): string {
  if (bytes == null || Number.isNaN(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let value = bytes;
  let unit = "B";
  for (const u of units) {
    if (value < 1024) break;
    value /= 1024;
    unit = u;
  }
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${unit}`;
}