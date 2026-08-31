/**
 * agentSettings.js
 * ---------------------------------------------------------------------
 * Paramètres de l'assistant IA : constantes, mapping camelCase/snake_case,
 * persistance localStorage et appels API dédiés (`/api/agent/*`).
 *
 * Module NON critique : il est consommé par SettingsPage / AppProvider mais
 * ne contient aucun endpoint du premier rendu. Séparé de sentimentApiClient.js
 * pour maintenir petit le chemin critique (le transport seul vit dans
 * clientCore.js). N'importe que le cœur de transport, jamais les endpoints.
 */

import { SentimentApiClientCore } from "./clientCore";

// --- Constantes de l'agent IA -------------------------------------------------

export const AGENT_PROVIDER_DEFAULT = "ollama";
export const AGENT_MODEL_DEFAULT = "";
export const AGENT_OLLAMA_URL_DEFAULT = "";
export const AGENT_OPENROUTER_URL_DEFAULT = "https://openrouter.ai/api/v1";
export const AGENT_OPENROUTER_API_KEY_DEFAULT = "";
export const AGENT_TIMEOUT_SECONDS_DEFAULT = 60;
export const AGENT_CONTEXT_LENGTH_DEFAULT = 512;
export const AGENT_TEMPERATURE_DEFAULT = 0.2;
export const AGENT_PROVIDERS = ["ollama", "openrouter"];
export const AGENT_SETTINGS_STORAGE_KEY = "thinktuning.agentSettings";
export const AGENT_LAST_MODEL_STORAGE_KEY = "thinktuning.agentLastModel";
export const AGENT_LAST_MODEL_DEFAULT = "";

/**
 * Convertit des paramètres agent en camelCase (formulaire du dashboard) en
 * corps snake_case attendu par l'API (`/api/agent/settings` en PUT).
 * Seules les clés présentes (non `undefined`) sont envoyées, ce qui permet les
 * mises à jour partielles (champ absent = inchangé côté serveur).
 */
export function agentSettingsPayload(input) {
  const src = input || {};
  const out = {};
  if (src.provider !== undefined) out.provider = src.provider;
  if (src.model !== undefined) out.model = src.model;
  if (src.ollamaUrl !== undefined) out.ollama_url = src.ollamaUrl;
  if (src.openrouterUrl !== undefined) out.openrouter_url = src.openrouterUrl;
  if (src.openrouterApiKey !== undefined) out.openrouter_api_key = src.openrouterApiKey;
  if (src.timeoutSeconds !== undefined && src.timeoutSeconds !== "") out.timeout_seconds = src.timeoutSeconds;
  if (src.contextLength !== undefined && src.contextLength !== "") out.context_length = src.contextLength;
  if (src.temperature !== undefined && src.temperature !== "") out.temperature = src.temperature;
  return out;
}

/**
 * Normalise des paramètres agent (snake_case depuis l'API, ou camelCase depuis
 * le formulaire / localStorage) en objet camelCase complet, prêt pour l'UI.
 * La clé OpenRouter n'étant jamais renvoyée par l'API, elle reste vide ici ;
 * le front réinjecte la valeur locale au besoin.
 */
export function normalizeAgentSettings(input) {
  const src = input || {};
  return {
    provider: src.provider || AGENT_PROVIDER_DEFAULT,
    model: src.model || AGENT_MODEL_DEFAULT,
    ollamaUrl: (src.ollamaUrl ?? src.ollama_url) || AGENT_OLLAMA_URL_DEFAULT,
    openrouterUrl: (src.openrouterUrl ?? src.openrouter_url) || AGENT_OPENROUTER_URL_DEFAULT,
    openrouterApiKey: (src.openrouterApiKey ?? src.openrouter_api_key) || AGENT_OPENROUTER_API_KEY_DEFAULT,
    hasOpenrouterApiKey: Boolean(src.has_openrouter_api_key ?? src.hasOpenrouterApiKey ?? false),
    timeoutSeconds: src.timeoutSeconds ?? src.timeout_seconds ?? AGENT_TIMEOUT_SECONDS_DEFAULT,
    contextLength: src.contextLength ?? src.context_length ?? AGENT_CONTEXT_LENGTH_DEFAULT,
    temperature: src.temperature ?? AGENT_TEMPERATURE_DEFAULT,
  };
}
/**
 * Charge les paramètres agent depuis localStorage.
 * @returns {Object} config agent
 */
function loadAgentSettingsFromStorage() {
  try {
    const raw = window.localStorage.getItem(AGENT_SETTINGS_STORAGE_KEY);
    if (!raw) return loadAgentSettingsFromDefaults();
    const parsed = JSON.parse(raw);
    return {
      provider: AGENT_PROVIDER_DEFAULT,
      model: AGENT_MODEL_DEFAULT,
      ollamaUrl: AGENT_OLLAMA_URL_DEFAULT,
      openrouterUrl: AGENT_OPENROUTER_URL_DEFAULT,
      openrouterApiKey: AGENT_OPENROUTER_API_KEY_DEFAULT,
      timeoutSeconds: AGENT_TIMEOUT_SECONDS_DEFAULT,
      contextLength: AGENT_CONTEXT_LENGTH_DEFAULT,
      temperature: AGENT_TEMPERATURE_DEFAULT,
      ...parsed,
    };
  } catch {
    return loadAgentSettingsFromDefaults();
  }
}

/**
 * Renvoie les valeurs par défaut couplées à l'env (si présent).
 */
function loadAgentSettingsFromDefaults() {
  const envProvider = import.meta.env.VITE_AGENT_PROVIDER
    ? import.meta.env.VITE_AGENT_PROVIDER.toLowerCase()
    : AGENT_PROVIDER_DEFAULT;
  const url = (key) => import.meta.env[`VITE_${key}`] || "";
  return {
    provider: AGENT_PROVIDERS.includes(envProvider) ? envProvider : AGENT_PROVIDER_DEFAULT,
    model: "",
    ollamaUrl: url("VITE_AGENT_OLLAMA_URL"),
    openrouterUrl: url("VITE_AGENT_OPENROUTER_URL"),
    openrouterApiKey: url("VITE_OPENROUTER_API_KEY"),
    timeoutSeconds: parseInt(url("VITE_AGENT_TIMEOUT_SECONDS"), 10) || AGENT_TIMEOUT_SECONDS_DEFAULT,
    contextLength: parseInt(url("VITE_AGENT_CONTEXT_LENGTH"), 10) || AGENT_CONTEXT_LENGTH_DEFAULT,
    temperature: parseFloat(url("VITE_AGENT_TEMPERATURE")) || AGENT_TEMPERATURE_DEFAULT,
  };
}

/**
 * Sauvegarde les paramètres agent dans localStorage.
 */
function saveAgentSettingsToStorage(settings) {
  try {
    window.localStorage.setItem(AGENT_SETTINGS_STORAGE_KEY, JSON.stringify(settings));
  } catch {
    /* stockage indisponible */
  }
}

/**
 * Charge le dernier modèle utilisé par l'assistant depuis localStorage.
 */
/**
 * Charge et prépare les paramètres agent pour être passés à l'API.
 * @returns {Object} payload prêt pour envoyer au serveur
 */
export function getAgentSettingsPayload() {
  const settings = loadAgentSettingsFromStorage();
  return agentSettingsPayload(settings);
}

/**
 * Récupère les paramètres agent depuis l'API.
 * @param {Object} apiConfig - { baseUrl, apiKey }
 * @returns {Promise<Object>} réponse /api/agent/settings
 */
export async function fetchAgentSettings(apiConfig) {
  const client = new SentimentApiClientCore(apiConfig);
  const payload = getAgentSettingsPayload();
  return client._request("/api/agent/settings", {
    method: "GET",
    body: payload,
  });
}

/**
 * Enregistre les paramètres agent via l'API.
 * @param {Object} apiConfig - { baseUrl, apiKey }
 * @param {Object} settingsPayload - { provider, model, ollamaUrl, openrouterUrl, openrouterApiKey, timeoutSeconds, contextLength, temperature }
 * @returns {Promise<Object>} réponse /api/agent/settings (en PUT)
 */
export async function updateAgentSettings(apiConfig, settingsPayload) {
  const client = new SentimentApiClientCore(apiConfig);
  const payload = agentSettingsPayload(settingsPayload);
  const response = await client._request("/api/agent/settings", {
    method: "PUT",
    body: payload,
  });
  if (response) {
    // Mise à jour locale immédiate (seq local → serveur)
    saveAgentSettingsToStorage(agentSettingsPayload(settingsPayload));
  }
  return response;
}

/**
 * Teste la connexion d'un pair de provider / modèle via l'API.
 * @param {Object} apiConfig - { baseUrl, apiKey }
 * @param {Object} testPayload - { provider, [ollamaUrl], [openrouterUrl], [openrouterApiKey], [model] }
 * @returns {Promise<Object>} réponse /api/agent/settings/test
 */
export async function testAgentConnection(apiConfig, testPayload) {
  const client = new SentimentApiClientCore(apiConfig);
  return client._request("/api/agent/settings/test", {
    method: "POST",
    body: agentSettingsPayload(testPayload),
  });
}

/**
 * Récupère le dernier modèle utilisé par l'assistant.
 */
export function getLastAgentModel() {
  return loadLastAgentModelFromStorage();
}

/**
 * Met à jour le dernier modèle utilisé par l'assistant.
 */
export function setLastAgentModel(model) {
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
function loadLastAgentModelFromStorage() {
  try {
    const raw = window.localStorage.getItem(AGENT_LAST_MODEL_STORAGE_KEY);
    return raw || AGENT_LAST_MODEL_DEFAULT;
  } catch {
    return AGENT_LAST_MODEL_DEFAULT;
  }
}