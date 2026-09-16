/**
 * chatTransport.ts — L3 (SCRUM-154)
 * ---------------------------------------------------------------------
 * Résolution de transport du chat (URL, authentification, helpers).
 *
 * Extrait de ChatWindow lors du découpage en hooks (`useAssistantTurns`,
 * `useChatApprovals`) : un seul endroit définit comment le chat joint le
 * backend et comment les erreurs HTTP sont converties en messages lisibles.
 */

import { DEFAULT_BASE_URL } from "../../api/clientCore";
import { readStoredSession, isSessionValid } from "../../api/authSession";

/** Endpoint du chat simple (streaming SSE ou JSON). */
export const AI_ENDPOINT = '/api/v1/chat/ai';

/**
 * Endpoint d'orchestration multi-agents (POST /api/v1/agent/multi/ask/stream) :
 * le superviseur planifie, dispatche des sous-tâches à des workers isolés puis
 * synthétise. Événements SSE nommés agent.plan / agent.worker.* / agent.done.
 */
export const MULTI_ASK_STREAM_ENDPOINT = '/api/v1/agent/multi/ask/stream';

/** Mode SSE demandé : les événements d'observabilité (worker.tool) sont filtrés. */
export const MULTI_SSE_MODE = 'compact';

/**
 * Endpoints du NOYAU agentique v2 — chemin unique du mode Agent :
 *  - POST /api/v1/agent/ask/core/stream : streaming SSE (core_tool / delta /
 *    final) — chemin principal ;
 *  - POST /api/v1/agent/ask/core : bloquant — repli si le backend ne connaît
 *    pas encore le stream (404/405).
 */
export const CORE_ASK_STREAM_ENDPOINT = '/api/v1/agent/ask/core/stream';
export const CORE_ASK_ENDPOINT = '/api/v1/agent/ask/core';

/** Base des endpoints de validation humaine (approve / reject). */
export const APPROVALS_ENDPOINT = '/api/v1/agent/approvals';

/** Endpoint des conversations persistées (GET/POST /api/v1/sessions…). */
export const SESSIONS_ENDPOINT = '/api/v1/sessions';

/** Endpoint listant les modèles LLM disponibles. */
export const MODELS_ENDPOINT = '/api/v1/chat/models';

/** Clé de stockage partagée avec le dashboard (config connexion). */
export const API_CONFIG_STORAGE_KEY = 'thinktuning.apiConfig';

/** Identifiant unique de message, avec repli pour les navigateurs anciens. */
export function createId(): string {
  return typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `msg-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`;
}

/** Horodatage ISO 8601 UTC (createdAt des messages). */
export function nowIso(): string {
  return new Date().toISOString();
}

/**
 * Récupère la clé API attendue par le backend (en-tête X-API-Key).
 *
 * Source principale : la configuration du dashboard persistée en localStorage
 * (champ « API_KEY côté serveur » du formulaire Configuration) ; repli : la
 * variable d'environnement Vite VITE_API_KEY. Résolue à chaque envoi afin de
 * prendre en compte un changement de configuration sans recharger la page.
 */
export function resolveApiKey(): string {
  try {
    const raw = window.localStorage.getItem(API_CONFIG_STORAGE_KEY);
    const apiKey = raw ? (JSON.parse(raw) as { apiKey?: string }).apiKey : undefined;
    if (apiKey) return apiKey;
  } catch {
    /* stockage indisponible ou JSON invalide : on utilise le repli ci-dessous */
  }
  return import.meta.env.VITE_API_KEY ?? '';
}

/**
 * Résout les EN-TÊTES d'authentification des appels fetch du chat.
 *
 * Session JWT en priorité (parité avec clientCore._headers et le backend,
 * qui valide le Bearer avant la clé) ; sinon repli historique X-API-Key
 * (config dashboard en mémoire / VITE_API_KEY). Résolu à CHAQUE envoi.
 */
export function resolveAuthHeaders(): Record<string, string> {
  const session = readStoredSession();
  if (session && isSessionValid(session)) {
    return { Authorization: `${session.tokenType || 'Bearer'} ${session.token}` };
  }
  const apiKey = resolveApiKey();
  return apiKey ? { 'X-API-Key': apiKey } : {};
}

/**
 * Résout la base URL de l'API (tous les appels fetch du chat sont préfixés).
 *
 * Source principale : la configuration du dashboard persistée en localStorage
 * (champ « URL de l'API ») ; repli : VITE_API_URL (et son défaut local).
 * Base vide = chemins relatifs (proxy nginx Docker, etc.).
 */
export function resolveBaseUrl(): string {
  try {
    const raw = window.localStorage.getItem(API_CONFIG_STORAGE_KEY);
    const baseUrl = raw ? (JSON.parse(raw) as { baseUrl?: string }).baseUrl : undefined;
    if (baseUrl) return baseUrl.replace(/\/+$/, '');
  } catch {
    /* stockage indisponible ou JSON invalide : on utilise le repli ci-dessous */
  }
  return DEFAULT_BASE_URL.replace(/\/+$/, '');
}

/**
 * Extrait le message d'erreur de l'enveloppe v1 (`error.message`) d'une
 * réponse non-OK. Repli sur un message générique si le corps n'est pas JSON
 * ou sans `error.message`.
 */
export async function apiErrorMessage(response: Response): Promise<string> {
  const generic = `Le serveur a répondu ${response.status} (${response.statusText})`;
  try {
    const data = await response.json();
    const error = (data as { error?: { message?: unknown } })?.error;
    if (error && typeof error.message === 'string' && error.message) {
      return error.message;
    }
  } catch {
    /* corps non-JSON : on garde le message générique */
  }
  return generic;
}
