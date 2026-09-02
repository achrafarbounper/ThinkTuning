/**
 * agentSettings.ts
 * ---------------------------------------------------------------------
 * Paramètres de l'assistant IA : constantes, mapping camelCase/snake_case,
 * persistance localStorage et appels API dédiés (`/api/agent/*`).
 *
 * Module NON critique : il est consommé par SettingsPage / AppProvider mais
 * ne contient aucun endpoint du premier rendu. Séparé de sentimentApiClient.ts
 * pour maintenir petit le chemin critique (le transport seul vit dans
 * clientCore.ts). N'importe que le cœur de transport, jamais les endpoints.
 */

import { SentimentApiClientCore, type ApiConfig } from "./clientCore";

// --- Constantes de l'agent IA -------------------------------------------------

export const AGENT_PROVIDER_DEFAULT = "ollama";
export const AGENT_MODEL_DEFAULT = "";
export const AGENT_OLLAMA_URL_DEFAULT = "";
export const AGENT_OPENROUTER_URL_DEFAULT = "https://openrouter.ai/api/v1";
export const AGENT_OPENROUTER_API_KEY_DEFAULT = "";
export const AGENT_HF_URL_DEFAULT = "https://router.huggingface.co/v1";
export const AGENT_HF_API_KEY_DEFAULT = "";
export const AGENT_TIMEOUT_SECONDS_DEFAULT = 60;
export const AGENT_CONTEXT_LENGTH_DEFAULT = 512;
export const AGENT_TEMPERATURE_DEFAULT = 0.2;
export const AGENT_PROVIDERS = ["ollama", "openrouter", "hf"] as const;
export const AGENT_SETTINGS_STORAGE_KEY = "thinktuning.agentSettings";
export const AGENT_LAST_MODEL_STORAGE_KEY = "thinktuning.agentLastModel";
export const AGENT_LAST_MODEL_DEFAULT = "";

export type AgentProvider = (typeof AGENT_PROVIDERS)[number] | string;

/** Paramètres de l'agent, en camelCase (format UI / localStorage).
 *  Les champs numériques acceptent aussi des chaînes (formulaires non convertis). */
export interface AgentSettings {
  provider: AgentProvider;
  model: string;
  ollamaUrl: string;
  openrouterUrl: string;
  openrouterApiKey: string;
  hfUrl: string;
  hfApiKey: string;
  hasOpenrouterApiKey?: boolean;
  hasHfApiKey?: boolean;
  timeoutSeconds: number | string;
  contextLength: number | string;
  temperature: number | string;
}

/** Entrée partielle acceptée par agentSettingsPayload. */
export type AgentSettingsInput = Partial<AgentSettings>;

/** Corps snake_case attendu par l'API. */
export type AgentSettingsPayload = Record<string, unknown>;

/**
 * Convertit des paramètres agent en camelCase (formulaire du dashboard) en
 * corps snake_case attendu par l'API (`/api/agent/settings` en PUT).
 * Seules les clés présentes (non `undefined`) sont envoyées, ce qui permet les
 * mises à jour partielles (champ absent = inchangé côté serveur).
 */
export function agentSettingsPayload(input?: AgentSettingsInput): AgentSettingsPayload {
  const src = input || {};
  const out: AgentSettingsPayload = {};
  if (src.provider !== undefined) out.provider = src.provider;
  if (src.model !== undefined) out.model = src.model;
  if (src.ollamaUrl !== undefined) out.ollama_url = src.ollamaUrl;
  if (src.openrouterUrl !== undefined) out.openrouter_url = src.openrouterUrl;
  if (src.openrouterApiKey !== undefined) out.openrouter_api_key = src.openrouterApiKey;
  if (src.hfUrl !== undefined) out.hf_url = src.hfUrl;
  if (src.hfApiKey !== undefined) out.hf_api_key = src.hfApiKey;
  if (src.timeoutSeconds !== undefined && src.timeoutSeconds !== "")
    out.timeout_seconds = src.timeoutSeconds;
  if (src.contextLength !== undefined && src.contextLength !== "")
    out.context_length = src.contextLength;
  if (src.temperature !== undefined && src.temperature !== "")
    out.temperature = src.temperature;
  return out;
}

/**
 * Normalise des paramètres agent (snake_case depuis l'API, ou camelCase depuis
 * le formulaire / localStorage) en objet camelCase complet, prêt pour l'UI.
 * La clé OpenRouter n'étant jamais renvoyée par l'API, elle reste vide ici ;
 * le front réinjecte la valeur locale au besoin.
 */
export function normalizeAgentSettings(input?: Record<string, unknown>): AgentSettings {
  const src = input || {};
  return {
    provider: (src.provider as AgentProvider) || AGENT_PROVIDER_DEFAULT,
    model: (src.model as string) || AGENT_MODEL_DEFAULT,
    ollamaUrl: ((src.ollamaUrl ?? src.ollama_url) as string) || AGENT_OLLAMA_URL_DEFAULT,
    openrouterUrl:
      ((src.openrouterUrl ?? src.openrouter_url) as string) || AGENT_OPENROUTER_URL_DEFAULT,
    openrouterApiKey:
      ((src.openrouterApiKey ?? src.openrouter_api_key) as string) ||
      AGENT_OPENROUTER_API_KEY_DEFAULT,
    hasOpenrouterApiKey: Boolean(
      (src.has_openrouter_api_key ?? src.hasOpenrouterApiKey) ?? false
    ),
    hfUrl: ((src.hfUrl ?? src.hf_url) as string) || AGENT_HF_URL_DEFAULT,
    hfApiKey:
      ((src.hfApiKey ?? src.hf_api_key) as string) || AGENT_HF_API_KEY_DEFAULT,
    hasHfApiKey: Boolean((src.has_hf_api_key ?? src.hasHfApiKey) ?? false),
    timeoutSeconds:
      ((src.timeoutSeconds ?? src.timeout_seconds) as string | number | undefined) ??
      AGENT_TIMEOUT_SECONDS_DEFAULT,
    contextLength:
      ((src.contextLength ?? src.context_length) as string | number | undefined) ??
      AGENT_CONTEXT_LENGTH_DEFAULT,
    temperature: (src.temperature as string | number | undefined) ?? AGENT_TEMPERATURE_DEFAULT,
  };
}
// --- Persistance localStorage -------------------------------------------------

/** Valeurs par défaut couplées à l'env (VITE_AGENT_*). */
function loadAgentSettingsFromDefaults(): AgentSettings {
  const envProvider = import.meta.env.VITE_AGENT_PROVIDER
    ? String(import.meta.env.VITE_AGENT_PROVIDER).toLowerCase()
    : AGENT_PROVIDER_DEFAULT;
  const url = (key: string): string => import.meta.env[`VITE_${key}`] || "";
  return {
    provider: (AGENT_PROVIDERS as readonly string[]).includes(envProvider)
      ? envProvider
      : AGENT_PROVIDER_DEFAULT,
    model: "",
    ollamaUrl: url("VITE_AGENT_OLLAMA_URL"),
    openrouterUrl: url("VITE_AGENT_OPENROUTER_URL"),
    openrouterApiKey: url("VITE_OPENROUTER_API_KEY"),
    hfUrl: url("VITE_AGENT_HF_URL"),
    hfApiKey: url("VITE_HF_API_KEY"),
    timeoutSeconds: parseInt(url("VITE_AGENT_TIMEOUT_SECONDS"), 10) || AGENT_TIMEOUT_SECONDS_DEFAULT,
    contextLength: parseInt(url("VITE_AGENT_CONTEXT_LENGTH"), 10) || AGENT_CONTEXT_LENGTH_DEFAULT,
    temperature: parseFloat(url("VITE_AGENT_TEMPERATURE")) || AGENT_TEMPERATURE_DEFAULT,
  };
}

/** Charge les paramètres agent depuis localStorage. */
function loadAgentSettingsFromStorage(): AgentSettings {
  try {
    const raw = window.localStorage.getItem(AGENT_SETTINGS_STORAGE_KEY);
    if (!raw) return loadAgentSettingsFromDefaults();
    const parsed = JSON.parse(raw) as Partial<AgentSettings>;
    return {
      provider: AGENT_PROVIDER_DEFAULT,
      model: AGENT_MODEL_DEFAULT,
      ollamaUrl: AGENT_OLLAMA_URL_DEFAULT,
      openrouterUrl: AGENT_OPENROUTER_URL_DEFAULT,
      openrouterApiKey: AGENT_OPENROUTER_API_KEY_DEFAULT,
      hfUrl: AGENT_HF_URL_DEFAULT,
      hfApiKey: AGENT_HF_API_KEY_DEFAULT,
      hasHfApiKey: false,
      timeoutSeconds: AGENT_TIMEOUT_SECONDS_DEFAULT,
      contextLength: AGENT_CONTEXT_LENGTH_DEFAULT,
      temperature: AGENT_TEMPERATURE_DEFAULT,
      ...parsed,
    };
  } catch {
    return loadAgentSettingsFromDefaults();
  }
}

/** Sauvegarde les paramètres agent dans localStorage. */
function saveAgentSettingsToStorage(settings: AgentSettings): void {
  try {
    window.localStorage.setItem(AGENT_SETTINGS_STORAGE_KEY, JSON.stringify(settings));
  } catch {
    /* stockage indisponible */
  }
}

/** Charge et prépare les paramètres agent pour être passés à l'API. */
export function getAgentSettingsPayload(): AgentSettingsPayload {
  const settings = loadAgentSettingsFromStorage();
  return agentSettingsPayload(settings);
}

/** Récupère les paramètres agent depuis l'API. */
export async function fetchAgentSettings(apiConfig: ApiConfig): Promise<unknown> {
  const client = new SentimentApiClientCore(apiConfig);
  const payload = getAgentSettingsPayload();
  return client._request("/api/agent/settings", {
    method: "GET",
    body: payload,
  });
}

/** Enregistre les paramètres agent via l'API. */
export async function updateAgentSettings(
  apiConfig: ApiConfig,
  settingsPayload: AgentSettingsInput
): Promise<unknown> {
  const client = new SentimentApiClientCore(apiConfig);
  const payload = agentSettingsPayload(settingsPayload);
  const response = await client._request("/api/agent/settings", {
    method: "PUT",
    body: payload,
  });
  if (response) {
    // Mise à jour locale immédiate (seq local → serveur)
    saveAgentSettingsToStorage(settingsPayload as AgentSettings);
  }
  return response;
}

/** Teste la connexion d'un provider / modèle via l'API. */
export async function testAgentConnection(
  apiConfig: ApiConfig,
  testPayload: AgentSettingsInput
): Promise<unknown> {
  const client = new SentimentApiClientCore(apiConfig);
  return client._request("/api/agent/settings/test", {
    method: "POST",
    body: agentSettingsPayload(testPayload),
  });
}

/** Récupère le dernier modèle utilisé par l'assistant. */
export function getLastAgentModel(): string {
  try {
    return (
      window.localStorage.getItem(AGENT_LAST_MODEL_STORAGE_KEY) || AGENT_LAST_MODEL_DEFAULT
    );
  } catch {
    return AGENT_LAST_MODEL_DEFAULT;
  }
}

/** Met à jour le dernier modèle utilisé par l'assistant. */
export function setLastAgentModel(model: string): void {
  try {
    if (!model) {
      window.localStorage.removeItem(AGENT_LAST_MODEL_STORAGE_KEY);
    } else {
      window.localStorage.setItem(AGENT_LAST_MODEL_STORAGE_KEY, model);
    }
  } catch {
    /* stockage indisponible */
  }
}
