/**
 * agentSettings.ts
 * ---------------------------------------------------------------------
 * Paramètres de l'assistant IA : constantes, mapping camelCase/snake_case,
 * persistance localStorage et appels API dédiés (`/api/v1/agent/*`).
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
export const AGENT_LM_STUDIO_URL_DEFAULT = "http://192.168.1.184:1234/v1";
export const AGENT_TIMEOUT_SECONDS_DEFAULT = 600;
export const AGENT_CONTEXT_LENGTH_DEFAULT = 2048;
export const AGENT_TEMPERATURE_DEFAULT = 0.2;
export const TRAIN_MAX_PER_LANG_DEFAULT = 500;
export const TRAIN_AUGMENT_FRACTION_DEFAULT = 0.4;
export const TRAIN_VARIANTS_PER_EXAMPLE_DEFAULT = 2;
export const TRAIN_USE_BACK_TRANSLATION_DEFAULT = false;
export const TRAIN_EPOCHS_DEFAULT = 4;
export const TRAIN_BATCH_SIZE_DEFAULT = 8;
export const TRAIN_NUM_WORKERS_DEFAULT = 0;
export const TRAIN_MAX_LENGTH_DEFAULT = 160;
export const TRAIN_LEARNING_RATE_DEFAULT = 3e-5;
export const TRAIN_WEIGHT_DECAY_DEFAULT = 0.01;
export const TRAIN_WARMUP_RATIO_DEFAULT = 0.1;
export const TRAIN_DEVICE_DEFAULT = "auto";
// SCRUM-138 : budgets, log, MCP et flags sont des réglages du module IHM,
// stockés/chargés depuis MongoDB (déplacés hors de app/config/settings.py).
export const AGENT_MAX_LLM_ROUNDS_DEFAULT = 6;
export const AGENT_MAX_TOOL_CALLS_DEFAULT = 20;
export const AGENT_LOG_LEVEL_DEFAULT = "INFO";
export const AGENT_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"] as const;
/** Feature flags (convention ``AGENT_<NOM>`` historique) — persistés en base. */
export const AGENT_FLAGS = [
  "reliability",
  "audit",
  "tool_analytics",
  "context",
  "copilot",
  "websocket",
  "multi_agent",
  "custom_tools",
  "new_core",
  "llm_v2",
] as const;
export const AGENT_PROVIDERS = ["ollama", "openrouter", "hf", "lm_studio"] as const;
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
  /** Racine de l'API LM Studio (serveur local, aucune clé requise). */
  lmStudioUrl: string;
  hasOpenrouterApiKey?: boolean;
  hasHfApiKey?: boolean;
  timeoutSeconds: number | string;
  contextLength: number | string;
  temperature: number | string;
  trainMaxPerLang: number | string;
  trainAugmentFraction: number | string;
  trainVariantsPerExample: number | string;
  trainUseBackTranslation: boolean;
  trainEpochs: number | string;
  trainBatchSize: number | string;
  trainNumWorkers: number | string;
  trainMaxLength: number | string;
  trainLearningRate: number | string;
  trainWeightDecay: number | string;
  trainWarmupRatio: number | string;
  trainDevice: string;
  // --- Budgets & garde-fous (module IHM / MongoDB) --------------------------
  maxLlmRounds: number | string;
  maxToolCalls: number | string;
  // --- Observabilité ---------------------------------------------------------
  logLevel: string;
  // --- Surface MCP -----------------------------------------------------------
  mcpFirst: boolean;
  mcpAuthRequired: boolean;
  // --- Feature flags (AGENT_<NOM> historique) --------------------------------
  flagReliability: boolean;
  flagAudit: boolean;
  flagToolAnalytics: boolean;
  flagContext: boolean;
  flagCopilot: boolean;
  flagWebsocket: boolean;
  flagMultiAgent: boolean;
  flagCustomTools: boolean;
  flagNewCore: boolean;
  flagLlmV2: boolean;
}

/** Entrée partielle acceptée par agentSettingsPayload. */
export type AgentSettingsInput = Partial<AgentSettings>;

/** Corps snake_case attendu par l'API. */
export type AgentSettingsPayload = Record<string, unknown>;

/**
 * Convertit un nom de flag snake_case (`new_core`) en clé camelCase UI
 * (`flagNewCore`) — convention `flag_<nom>` ↔ `flag<Nom>` (AGENT_<NOM>).
 */
export function agentFlagCamelCase(name: string): string {
  const [head, ...rest] = name.split("_");
  const capital = (part: string) => (part[0] || "").toUpperCase() + part.slice(1);
  return "flag" + capital(head) + rest.map(capital).join("");
}

/**
 * Convertit des paramètres agent en camelCase (formulaire du dashboard) en
 * corps snake_case attendu par l'API (`/api/v1/agent/settings` en PUT).
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
  if (src.lmStudioUrl !== undefined) out.lm_studio_url = src.lmStudioUrl;
  if (src.timeoutSeconds !== undefined && src.timeoutSeconds !== "")
    out.timeout_seconds = src.timeoutSeconds;
  if (src.contextLength !== undefined && src.contextLength !== "")
    out.context_length = src.contextLength;
  if (src.temperature !== undefined && src.temperature !== "")
    out.temperature = src.temperature;
  if (src.trainMaxPerLang !== undefined && src.trainMaxPerLang !== "")
    out.train_max_per_lang = src.trainMaxPerLang;
  if (src.trainAugmentFraction !== undefined && src.trainAugmentFraction !== "")
    out.train_augment_fraction = src.trainAugmentFraction;
  if (src.trainVariantsPerExample !== undefined && src.trainVariantsPerExample !== "")
    out.train_variants_per_example = src.trainVariantsPerExample;
  if (src.trainUseBackTranslation !== undefined)
    out.train_use_back_translation = src.trainUseBackTranslation;
  if (src.trainEpochs !== undefined && src.trainEpochs !== "")
    out.train_epochs = src.trainEpochs;
  if (src.trainBatchSize !== undefined && src.trainBatchSize !== "")
    out.train_batch_size = src.trainBatchSize;
  if (src.trainNumWorkers !== undefined && src.trainNumWorkers !== "")
    out.train_num_workers = src.trainNumWorkers;
  if (src.trainMaxLength !== undefined && src.trainMaxLength !== "")
    out.train_max_length = src.trainMaxLength;
  if (src.trainLearningRate !== undefined && src.trainLearningRate !== "")
    out.train_learning_rate = src.trainLearningRate;
  if (src.trainWeightDecay !== undefined && src.trainWeightDecay !== "")
    out.train_weight_decay = src.trainWeightDecay;
  if (src.trainWarmupRatio !== undefined && src.trainWarmupRatio !== "")
    out.train_warmup_ratio = src.trainWarmupRatio;
  if (src.trainDevice !== undefined && src.trainDevice !== "")
    out.train_device = src.trainDevice;
  // SCRUM-138 : budgets, log, MCP et flags — clés du module IHM (base MongoDB).
  if (src.maxLlmRounds !== undefined && src.maxLlmRounds !== "")
    out.max_llm_rounds = src.maxLlmRounds;
  if (src.maxToolCalls !== undefined && src.maxToolCalls !== "")
    out.max_tool_calls = src.maxToolCalls;
  if (src.logLevel !== undefined && src.logLevel !== "") out.log_level = src.logLevel;
  if (src.mcpFirst !== undefined) out.mcp_first = src.mcpFirst;
  if (src.mcpAuthRequired !== undefined) out.mcp_auth_required = src.mcpAuthRequired;
  for (const name of AGENT_FLAGS) {
    const camel = agentFlagCamelCase(name);
    if (src[camel as keyof AgentSettings] !== undefined)
      out[`flag_${name}`] = src[camel as keyof AgentSettings];
  }
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
  const flag = (camel: string, snake: string, fallback: boolean): boolean =>
    (src[camel] !== undefined ? src[camel] : src[snake]) === undefined
      ? fallback
      : Boolean(src[camel] !== undefined ? src[camel] : src[snake]);
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
    lmStudioUrl:
      ((src.lmStudioUrl ?? src.lm_studio_url) as string) || AGENT_LM_STUDIO_URL_DEFAULT,
    timeoutSeconds:
      ((src.timeoutSeconds ?? src.timeout_seconds) as string | number | undefined) ??
      AGENT_TIMEOUT_SECONDS_DEFAULT,
    contextLength:
      ((src.contextLength ?? src.context_length) as string | number | undefined) ??
      AGENT_CONTEXT_LENGTH_DEFAULT,
    temperature: (src.temperature as string | number | undefined) ?? AGENT_TEMPERATURE_DEFAULT,
    trainMaxPerLang:
      ((src.trainMaxPerLang ?? src.train_max_per_lang) as string | number | undefined) ??
      TRAIN_MAX_PER_LANG_DEFAULT,
    trainAugmentFraction:
      ((src.trainAugmentFraction ?? src.train_augment_fraction) as string | number | undefined) ??
      TRAIN_AUGMENT_FRACTION_DEFAULT,
    trainVariantsPerExample:
      ((src.trainVariantsPerExample ?? src.train_variants_per_example) as string | number | undefined) ??
      TRAIN_VARIANTS_PER_EXAMPLE_DEFAULT,
    trainUseBackTranslation: flag(
      "trainUseBackTranslation",
      "train_use_back_translation",
      TRAIN_USE_BACK_TRANSLATION_DEFAULT
    ),
    trainEpochs:
      ((src.trainEpochs ?? src.train_epochs) as string | number | undefined) ??
      TRAIN_EPOCHS_DEFAULT,
    trainBatchSize:
      ((src.trainBatchSize ?? src.train_batch_size) as string | number | undefined) ??
      TRAIN_BATCH_SIZE_DEFAULT,
    trainNumWorkers:
      ((src.trainNumWorkers ?? src.train_num_workers) as string | number | undefined) ??
      TRAIN_NUM_WORKERS_DEFAULT,
    trainMaxLength:
      ((src.trainMaxLength ?? src.train_max_length) as string | number | undefined) ??
      TRAIN_MAX_LENGTH_DEFAULT,
    trainLearningRate:
      ((src.trainLearningRate ?? src.train_learning_rate) as string | number | undefined) ??
      TRAIN_LEARNING_RATE_DEFAULT,
    trainWeightDecay:
      ((src.trainWeightDecay ?? src.train_weight_decay) as string | number | undefined) ??
      TRAIN_WEIGHT_DECAY_DEFAULT,
    trainWarmupRatio:
      ((src.trainWarmupRatio ?? src.train_warmup_ratio) as string | number | undefined) ??
      TRAIN_WARMUP_RATIO_DEFAULT,
    trainDevice:
      ((src.trainDevice ?? src.train_device) as string | undefined) || TRAIN_DEVICE_DEFAULT,
    // SCRUM-138 : budgets, log, MCP et flags du module IHM (base MongoDB).
    maxLlmRounds:
      ((src.maxLlmRounds ?? src.max_llm_rounds) as string | number | undefined) ??
      AGENT_MAX_LLM_ROUNDS_DEFAULT,
    maxToolCalls:
      ((src.maxToolCalls ?? src.max_tool_calls) as string | number | undefined) ??
      AGENT_MAX_TOOL_CALLS_DEFAULT,
    logLevel:
      ((src.logLevel ?? src.log_level) as string | undefined) || AGENT_LOG_LEVEL_DEFAULT,
    mcpFirst: flag("mcpFirst", "mcp_first", false),
    mcpAuthRequired: flag("mcpAuthRequired", "mcp_auth_required", true),
    flagReliability: flag("flagReliability", "flag_reliability", true),
    flagAudit: flag("flagAudit", "flag_audit", true),
    flagToolAnalytics: flag("flagToolAnalytics", "flag_tool_analytics", true),
    flagContext: flag("flagContext", "flag_context", true),
    flagCopilot: flag("flagCopilot", "flag_copilot", true),
    flagWebsocket: flag("flagWebsocket", "flag_websocket", true),
    flagMultiAgent: flag("flagMultiAgent", "flag_multi_agent", true),
    flagCustomTools: flag("flagCustomTools", "flag_custom_tools", true),
    flagNewCore: flag("flagNewCore", "flag_new_core", true),
    flagLlmV2: flag("flagLlmV2", "flag_llm_v2", true),
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
    lmStudioUrl: url("VITE_AGENT_LM_STUDIO_URL"),
    timeoutSeconds: parseInt(url("VITE_AGENT_TIMEOUT_SECONDS"), 10) || AGENT_TIMEOUT_SECONDS_DEFAULT,
    contextLength: parseInt(url("VITE_AGENT_CONTEXT_LENGTH"), 10) || AGENT_CONTEXT_LENGTH_DEFAULT,
    temperature: parseFloat(url("VITE_AGENT_TEMPERATURE")) || AGENT_TEMPERATURE_DEFAULT,
    trainMaxPerLang: Number(import.meta.env.VITE_TRAIN_MAX_PER_LANG) || TRAIN_MAX_PER_LANG_DEFAULT,
    trainAugmentFraction:
      Number(import.meta.env.VITE_TRAIN_AUGMENT_FRACTION) || TRAIN_AUGMENT_FRACTION_DEFAULT,
    trainVariantsPerExample:
      Number(import.meta.env.VITE_TRAIN_VARIANTS_PER_EXAMPLE) || TRAIN_VARIANTS_PER_EXAMPLE_DEFAULT,
    trainUseBackTranslation: false,
    trainEpochs: Number(import.meta.env.VITE_TRAIN_EPOCHS) || TRAIN_EPOCHS_DEFAULT,
    trainBatchSize: Number(import.meta.env.VITE_TRAIN_BATCH_SIZE) || TRAIN_BATCH_SIZE_DEFAULT,
    trainNumWorkers: Number(import.meta.env.VITE_TRAIN_NUM_WORKERS) || TRAIN_NUM_WORKERS_DEFAULT,
    trainMaxLength: Number(import.meta.env.VITE_TRAIN_MAX_LENGTH) || TRAIN_MAX_LENGTH_DEFAULT,
    trainLearningRate:
      Number(import.meta.env.VITE_TRAIN_LEARNING_RATE) || TRAIN_LEARNING_RATE_DEFAULT,
    trainWeightDecay:
      Number(import.meta.env.VITE_TRAIN_WEIGHT_DECAY) || TRAIN_WEIGHT_DECAY_DEFAULT,
    trainWarmupRatio:
      Number(import.meta.env.VITE_TRAIN_WARMUP_RATIO) || TRAIN_WARMUP_RATIO_DEFAULT,
    trainDevice: import.meta.env.VITE_TRAIN_DEVICE || TRAIN_DEVICE_DEFAULT,
    maxLlmRounds: parseInt(url("VITE_AGENT_MAX_LLM_ROUNDS"), 10) || AGENT_MAX_LLM_ROUNDS_DEFAULT,
    maxToolCalls: parseInt(url("VITE_AGENT_MAX_TOOL_CALLS"), 10) || AGENT_MAX_TOOL_CALLS_DEFAULT,
    logLevel: url("VITE_AGENT_LOG_LEVEL") || AGENT_LOG_LEVEL_DEFAULT,
    mcpFirst: false,
    mcpAuthRequired: true,
    flagReliability: true,
    flagAudit: true,
    flagToolAnalytics: true,
    flagContext: true,
    flagCopilot: true,
    flagWebsocket: true,
    flagMultiAgent: true,
    flagCustomTools: true,
    flagNewCore: true,
    flagLlmV2: true,
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
      lmStudioUrl: AGENT_LM_STUDIO_URL_DEFAULT,
      timeoutSeconds: AGENT_TIMEOUT_SECONDS_DEFAULT,
      contextLength: AGENT_CONTEXT_LENGTH_DEFAULT,
      temperature: AGENT_TEMPERATURE_DEFAULT,
      trainMaxPerLang: TRAIN_MAX_PER_LANG_DEFAULT,
      trainAugmentFraction: TRAIN_AUGMENT_FRACTION_DEFAULT,
      trainVariantsPerExample: TRAIN_VARIANTS_PER_EXAMPLE_DEFAULT,
      trainUseBackTranslation: TRAIN_USE_BACK_TRANSLATION_DEFAULT,
      trainEpochs: TRAIN_EPOCHS_DEFAULT,
      trainBatchSize: TRAIN_BATCH_SIZE_DEFAULT,
      trainNumWorkers: TRAIN_NUM_WORKERS_DEFAULT,
      trainMaxLength: TRAIN_MAX_LENGTH_DEFAULT,
      trainLearningRate: TRAIN_LEARNING_RATE_DEFAULT,
      trainWeightDecay: TRAIN_WEIGHT_DECAY_DEFAULT,
      trainWarmupRatio: TRAIN_WARMUP_RATIO_DEFAULT,
      trainDevice: TRAIN_DEVICE_DEFAULT,
      maxLlmRounds: AGENT_MAX_LLM_ROUNDS_DEFAULT,
      maxToolCalls: AGENT_MAX_TOOL_CALLS_DEFAULT,
      logLevel: AGENT_LOG_LEVEL_DEFAULT,
      mcpFirst: false,
      mcpAuthRequired: true,
      flagReliability: true,
      flagAudit: true,
      flagToolAnalytics: true,
      flagContext: true,
      flagCopilot: true,
      flagWebsocket: true,
      flagMultiAgent: true,
      flagCustomTools: true,
      flagNewCore: true,
      flagLlmV2: true,
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
  return client._request("/api/v1/agent/settings", {
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
  const response = await client._request("/api/v1/agent/settings", {
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
  return client._request("/api/v1/agent/settings/test", {
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
