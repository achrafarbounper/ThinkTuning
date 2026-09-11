/**
 * api/authSession.ts
 * ---------------------------------------------------------------------
 * Session d'authentification navigateur (localStorage) + aides d'affichage
 * des erreurs. Le jeton JWT est émis par POST /api/v1/auth/token puis
 * réutilisé par le transport client (Authorization: Bearer) côté dashboard.
 *
 * Sécurité : le backend renvoie volontairement UN MESSAGE D'ERREUR UNIQUE
 * (« client_id ou secret invalide ») en cas d'échec, pour interdire toute
 * énumération de comptes. On affiche donc un équivalent court et sûr
 * (« Identifiants invalides ») au lieu de révéler quelle partie est fausse.
 */

import { ApiError } from "./clientCore";

/** Durée de vie demandée pour un jeton de session (24 h — plafond backend). */
export const AUTH_TTL_SECONDS = 86_400;

export const SESSION_KEY = "thinktuning.authSession";

export interface AuthSession {
  /** Identifiant du service account (valeur du champ « Email »). */
  clientId: string;
  token: string;
  tokenType: string;
  role: string;
  /** Estampille d'émission (epoch ms). */
  issuedAt: number;
  /** Durée de vie en secondes (champ expires_in du backend). */
  expiresIn: number;
}

/** Horodatage d'expiration de la session (epoch ms). */
export function sessionExpiresAt(session: AuthSession): number {
  return session.issuedAt + session.expiresIn * 1000;
}

/** True tant que la session existe et n'est pas expirée. */
export function isSessionValid(
  session: AuthSession | null | undefined,
  now: number = Date.now()
): boolean {
  return Boolean(session && session.token && sessionExpiresAt(session) > now);
}

/** Construit une session à partir de la réponse de POST /auth/token. */
export function buildAuthSession(
  clientId: string,
  token: string,
  options: {
    tokenType?: string;
    role?: string;
    expiresIn: number;
    issuedAt?: number;
  } = { expiresIn: AUTH_TTL_SECONDS }
): AuthSession {
  return {
    clientId,
    token,
    tokenType: options.tokenType ?? "Bearer",
    role: options.role ?? "read",
    issuedAt: options.issuedAt ?? Date.now(),
    expiresIn: options.expiresIn,
  };
}

/** Charge la session persistée (null si absente ou illisible). */
export function readStoredSession(): AuthSession | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<AuthSession>;
    if (
      typeof parsed?.token === "string" &&
      parsed.token &&
      typeof parsed.clientId === "string" &&
      typeof parsed.expiresIn === "number"
    ) {
      return {
        clientId: parsed.clientId,
        token: parsed.token,
        tokenType: parsed.tokenType ?? "Bearer",
        role: parsed.role ?? "read",
        issuedAt: parsed.issuedAt ?? Date.now(),
        expiresIn: parsed.expiresIn,
      };
    }
    return null;
  } catch {
    return null;
  }
}

/** URL de l'API configurée (thinktuning.apiConfig) — source de vérité. */
export function readStoredBaseUrl(fallback: string): string {
  if (typeof window === "undefined") return fallback;
  try {
    const parsed = JSON.parse(
      window.localStorage.getItem("thinktuning.apiConfig") ?? ""
    ) as Partial<{ baseUrl?: string }>;
    return typeof parsed?.baseUrl === "string" && parsed.baseUrl
      ? parsed.baseUrl
      : fallback;
  } catch {
    return fallback;
  }
}

/** Traduit une erreur réseau en message court, actionnable et sécurisé. */
export function mapAuthErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "Identifiants invalides";
    if (error.status === 0) return "API injoignable : vérifiez le serveur";
    if (typeof error.message === "string" && error.message) return error.message;
  }
  if (error instanceof Error && error.message) return error.message;
  return "Connexion impossible";
}