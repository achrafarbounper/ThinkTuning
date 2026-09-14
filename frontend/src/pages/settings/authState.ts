/**
 * pages/settings/authState.ts
 * ---------------------------------------------------------------------
 * Résolution de l'état d'authentification effectif pour la section
 * « Connexion API » (page Paramètres).
 *
 * Le transport client (api/clientCore) attache le Bearer JWT EN PRIORITÉ
 * sur X-API-Key : le badge distingue donc trois états et non plus deux,
 * afin d'expliquer pourquoi une clé saisie « disparaît » au rafraîchissement
 * (P1 SEC : aucun secret en localStorage) alors qu'une session JWT, elle,
 * persiste 24 h (thinktuning.authSession).
 *
 * Module purement fonctionnel (aucun React) — testé en Vitest.
 */

/** États d'authentification effectifs de la connexion API. */
export type AuthState =
  | "jwt" // Session JWT valide — persistante (24 h), Bearer prioritaire.
  | "api-key" // Clé API en mémoire seule — repli dev, non persistée.
  | "none"; // Aucune authentification effective.

/** Rendu du badge + aide contextuelle associés à un état. */
export interface AuthStateView {
  /** Étiquette courte du badge. */
  label: string;
  /** Classe de couleur du tag (convention tt-tag-status-* du CSS global). */
  tone: "completed" | "running" | "pending" | "failed" | "cancelled";
  /** Aide contextuelle affichée sous le formulaire. */
  hint: string;
}

/**
 * Résout l'état effectif : la session JWT est prioritaire (même règle de
 * transport que clientCore — Bearer avant X-API-Key). Les valeurs sont
 * trimées : un champ rempli d'espaces ne compte pas comme une
 * authentification présente.
 */
export function resolveAuthState(
  sessionToken: string | null | undefined,
  apiKey: string | null | undefined,
): AuthState {
  if (sessionToken && sessionToken.trim()) return "jwt";
  if (apiKey && apiKey.trim()) return "api-key";
  return "none";
}

/** Libellé / couleur / aide pour chaque état (source unique du rendu). */
const VIEWS: Record<AuthState, AuthStateView> = {
  jwt: {
    label: "session active",
    tone: "completed",
    hint:
      "Session JWT active (24 h, conservée) — le Bearer est prioritaire sur la " +
      "clé API et survit au rafraîchissement. La clé ci-dessous est inutile.",
  },
  "api-key": {
    label: "configurée (mémoire)",
    tone: "pending",
    hint:
      "Clé API utilisée en mémoire seule (repli dev sans proxy) : volontairement " +
      "non conservée au rafraîchissement — aucun secret n'est écrit dans le " +
      "navigateur (P1 SEC). Connectez-vous pour une session persistante.",
  },
  none: {
    label: "manquante",
    tone: "failed",
    hint:
      "Aucune authentification effective : saisissez une clé API (repli dev) ou " +
      "connectez-vous pour obtenir une session JWT persistante.",
  },
};

/** Retourne le rendu (libellé, couleur, aide) associé à un état. */
export function authStateView(state: AuthState): AuthStateView {
  return VIEWS[state];
}
