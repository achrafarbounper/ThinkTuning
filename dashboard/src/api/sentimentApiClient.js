/**
 * sentimentApiClient.js
 * ---------------------------------------------------------------------
 * Client HTTP minimal (fetch natif, sans dépendance) pour l'API
 * FastAPI de ThinkTuning (api.py).
 *
 * Toutes les routes exigent l'en-tête `X-API-Key`, SAUF :
 *   - GET /health
 *   - GET /metrics
 * (ce sont les deux seules routes de api.py sans Depends(require_api_key)).
 *
 * Usage :
 *   import { SentimentApiClient } from "./sentimentApiClient";
 *   const api = new SentimentApiClient({ baseUrl: "http://localhost:8000", apiKey: "..." });
 *   const { results } = await api.predict(["Ce produit est génial !"]);
 */

export const DEFAULT_BASE_URL =
  (typeof import.meta !== "undefined" &&
    import.meta.env &&
    import.meta.env.VITE_API_URL) ??
  "http://localhost:8000";

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

// --- Timeout réseau -----------------------------------------------------------

/** Délai par défaut d'une requête JSON (30 s). */
export const DEFAULT_TIMEOUT_MS = 30_000;
/** Délai par défaut d'un upload multipart (60 s, fichiers CSV potentiellement lourds). */
export const DEFAULT_MULTIPART_TIMEOUT_MS = 60_000;

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

export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
      }
}

/** Ordre réel des étapes traversées par _run_training() dans api.py.
 *  Exporté ici pour que l'UI (tracker de progression) reste alignée sur
 *  le vrai pipeline serveur au lieu d'une barre de progression inventée. */
export const TRAIN_STEPS = [
  "queued",
  "loading_dataset",
  "splitting_dataset",
  "augmenting_dataset",
  "building_dataloaders",
  "computing_class_weights",
  "loading_model",
  "training",
  "saving_model",
  "done",
];

/** Ordre des étapes du pipeline end-to-end (core/pipeline_runner.py). */
export const PIPELINE_STEPS = [
  "queued",
  "labeling",
  "filtering",
  "finetuning",
  "done",
];

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
function loadLastAgentModelFromStorage() {
  try {
    const raw = window.localStorage.getItem(AGENT_LAST_MODEL_STORAGE_KEY);
    return raw || AGENT_LAST_MODEL_DEFAULT;
  } catch {
    return AGENT_LAST_MODEL_DEFAULT;
  }
}

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
  const client = new SentimentApiClient(apiConfig);
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
  const client = new SentimentApiClient(apiConfig);
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
  const client = new SentimentApiClient(apiConfig);
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




export class SentimentApiClient {
  constructor({ baseUrl = DEFAULT_BASE_URL, apiKey = "" } = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.apiKey = apiKey || "";
  }

  setConfig({ baseUrl, apiKey } = {}) {
    if (baseUrl !== undefined) this.baseUrl = baseUrl.replace(/\/+$/, "");
    if (apiKey !== undefined) this.apiKey = apiKey;
  }

  _headers(isJson) {
    const headers = {};
    if (isJson) headers["Content-Type"] = "application/json";
    if (this.apiKey) headers["X-API-Key"] = this.apiKey;
    return headers;
  }

  _buildUrl(path, query) {
    let url = `${this.baseUrl}${path}`;
    if (query) {
      const entries = Object.entries(query).filter(
        ([, v]) => v !== undefined && v !== null && v !== ""
      );
      if (entries.length) {
        url += `?${new URLSearchParams(entries).toString()}`;
      }
    }
    return url;
  }

  async _request(path, { method = "GET", body, query, timeoutMs, signal } = {}) {
    const url = this._buildUrl(path, query);
    const init = { method, headers: this._headers(body !== undefined) };
    if (body !== undefined) init.body = JSON.stringify(body);

    const { controller, cleanup } = this._startRequest(timeoutMs, signal);
    init.signal = controller.signal;

    let response;
    try {
      response = await fetch(url, init);
    } catch (networkErr) {
      throw new ApiError(
        this._networkErrorMessage(
          networkErr,
          controller.signal.reason === "timeout"
        ),
        0,
        null
      );
    } finally {
      cleanup();
    }

    if (response.status === 204) return null;

    const contentType = response.headers.get("content-type") || "";
    const isJson = contentType.includes("application/json");
    const payload = isJson
      ? await response.json().catch(() => null)
      : await response.text();

    if (!response.ok) {
      const detail = isJson && payload ? payload.detail : payload;
      throw new ApiError(
        typeof detail === "string" && detail
          ? detail
          : `Erreur HTTP ${response.status} sur ${path}`,
        response.status,
        detail
      );
    }

    return payload;
  }

  async _requestMultipart(path, { formData, query, expectBlob = false, timeoutMs, signal } = {}) {
    const url = this._buildUrl(path, query);
    const headers = this.apiKey ? { "X-API-Key": this.apiKey } : {};

    const { controller, cleanup } = this._startRequest(
      timeoutMs ?? DEFAULT_MULTIPART_TIMEOUT_MS,
      signal
    );

    let response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers,
        body: formData,
        signal: controller.signal,
      });
    } catch (networkErr) {
      throw new ApiError(
        this._networkErrorMessage(
          networkErr,
          controller.signal.reason === "timeout"
        ),
        0,
        null
      );
    } finally {
      cleanup();
    }

    if (!response.ok) {
      let detail = `Erreur HTTP ${response.status} sur ${path}`;
      try {
        const errPayload = await response.json();
        detail = errPayload.detail || detail;
      } catch {
        /* réponse non-JSON, on garde le message générique */
      }
      throw new ApiError(detail, response.status, detail);
    }

    return expectBlob ? response.blob() : response.json();
  }

  /**
   * Prépare un AbortController couplant un timeout interne et un éventuel
   * signal fourni par l'appelant (annulation externe : bouton « Stop », etc.).
   * Les deux relais partagent le même contrôleur ; la cause « timeout » est
   * marquée dans `signal.reason` pour produire un message d'erreur distinct.
   */
  _startRequest(timeoutMs, signal) {
    const controller = new AbortController();
    const timeout = timeoutMs ?? DEFAULT_TIMEOUT_MS;
    let timer = null;
    if (timeout > 0) {
      timer = window.setTimeout(() => controller.abort("timeout"), timeout);
    }
    const onExternalAbort = () => controller.abort();
    if (signal) {
      if (signal.aborted) controller.abort();
      else signal.addEventListener("abort", onExternalAbort);
    }
    const cleanup = () => {
      if (timer !== null) window.clearTimeout(timer);
      signal?.removeEventListener("abort", onExternalAbort);
    };
    return { controller, cleanup };
  }

  _networkErrorMessage(err, isTimeout) {
    if (isTimeout) {
      return `La requête a dépassé le délai autorisé (annulée). Réessayez.`;
    }
    if (err && err.name === "AbortError") {
      return `La requête a été annulée.`;
    }
    return `Impossible de joindre l'API à ${this.baseUrl} (${err.message}). ` +
      `Vérifiez l'URL, que le serveur tourne, et la config CORS.`;
  }

  // -- /health, /metrics (sans authentification) --------------------------

  getHealth() {
    return this._request("/health");
  }

  async getMetricsRaw() {
    const url = this._buildUrl("/metrics");
    const response = await fetch(url);
    if (!response.ok) {
      throw new ApiError(`Erreur HTTP ${response.status} sur /metrics`, response.status);
    }
    return response.text();
  }

  /** Endpoint proxy JSON de secours (voir api/routes/metrics.py). */
  async getMetricsJson() {
    const url = this._buildUrl("/metrics/json");
    const response = await fetch(url);
    if (!response.ok) {
      throw new ApiError(`Erreur HTTP ${response.status} sur /metrics/json`, response.status);
    }
    return response.json();
  }

  // -- /models --------------------------------------------------------------

  listModels() {
    return this._request("/models/details");
  }

  // -- /evaluate ------------------------------------------------------------

  getConfusion({ model, limit } = {}) {
    return this._request("/evaluate/confusion", {
      query: { model, limit },
    });
  }

  // -- /predict ---------------------------------------------------------------

  predict(texts, model) {
    return this._request("/predict", {
      method: "POST",
      body: { texts },
      query: model ? { model } : undefined,
    });
  }

  predictBatchJson({ file, textColumn = "text", model } = {}) {
    const form = new FormData();
    form.append("file", file);
    form.append("text_column", textColumn);
    form.append("response_format", "json");
    return this._requestMultipart("/predict/batch", {
      formData: form,
      query: model ? { model } : undefined,
    });
  }

  predictBatchCsv({ file, textColumn = "text", model } = {}) {
    const form = new FormData();
    form.append("file", file);
    form.append("text_column", textColumn);
    form.append("response_format", "csv");
    return this._requestMultipart("/predict/batch", {
      formData: form,
      expectBlob: true,
      query: model ? { model } : undefined,
    });
  }

  // -- /drift ------------------------------------------------------------------

  /**
   * Détection de dérive entre deux batches, via deux fichiers CSV uploadés.
   * Retourne {method, drift_score, p_value, threshold, drift_detected,
   * distribution_a, distribution_b, n_a, n_b}.
   */
  driftCsv({ fileA, fileB, textColumn = "text", threshold, method, model } = {}) {
    const form = new FormData();
    form.append("file_a", fileA);
    form.append("file_b", fileB);
    form.append("text_column", textColumn);
    if (threshold !== undefined && threshold !== "") form.append("threshold", threshold);
    if (method) form.append("method", method);
    return this._requestMultipart("/drift", {
      formData: form,
      query: model ? { model } : undefined,
    });
  }

  /** Détection de dérive entre deux listes de textes (mode JSON). */
  driftTexts({ textsA, textsB, threshold, method, model } = {}) {
    const body = { texts_a: textsA, texts_b: textsB };
    if (threshold !== undefined && threshold !== "") body.threshold = threshold;
    if (method) body.method = method;
    return this._request("/drift", {
      method: "POST",
      body,
      query: model ? { model } : undefined,
    });
  }

  reloadPredictor(model) {
    return this._request("/predict/reload", {
      method: "POST",
      query: model ? { model } : undefined,
    });
  }

  // -- /explain ----------------------------------------------------------------

  /**
   * Génère une explication en langage naturel de la prédiction d'un texte
   * (via l'agent IA / provider OpenRouter) : {sentiment, confidence, explanation}.
   */
  explain({ text, model } = {}) {
    return this._request("/explain", {
      method: "POST",
      body: model ? { text, model } : { text },
    });
  }

  // -- /train -----------------------------------------------------------------

  startTraining(payload) {
    return this._request("/train", { method: "POST", body: payload });
  }

  getTrainingStatus(jobId) {
    return this._request(`/train/status/${encodeURIComponent(jobId)}`);
  }

  cancelTraining(jobId) {
    return this._request(`/train/cancel/${encodeURIComponent(jobId)}`, {
      method: "POST",
    });
  }

    listTrainingJobs({ status, limit, offset } = {}) {
    return this._request("/train/jobs", {
      query: { status, limit, offset },
    });
  }

  // -- /pipeline ---------------------------------------------------------------

  /** Lance le pipeline end-to-end (labeling -> filtering -> fine-tuning LLM). */
  startPipeline(payload) {
    return this._request("/pipeline", { method: "POST", body: payload });
  }

  getPipelineStatus(jobId) {
    return this._request(`/pipeline/status/${encodeURIComponent(jobId)}`);
  }

  cancelPipeline(jobId) {
    return this._request(`/pipeline/cancel/${encodeURIComponent(jobId)}`, {
      method: "POST",
    });
  }

  listPipelineJobs({ status, limit, offset } = {}) {
    return this._request("/pipeline/jobs", {
      query: { status, limit, offset },
    });
  }
}

export default SentimentApiClient;
