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

const DEFAULT_BASE_URL =
  (typeof import.meta !== "undefined" &&
    import.meta.env &&
    import.meta.env.VITE_API_URL) ||
  "http://localhost:8000";

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

  async _request(path, { method = "GET", body, query } = {}) {
    const url = this._buildUrl(path, query);
    const init = { method, headers: this._headers(body !== undefined) };
    if (body !== undefined) init.body = JSON.stringify(body);

    let response;
    try {
      response = await fetch(url, init);
    } catch (networkErr) {
      throw new ApiError(
        `Impossible de joindre l'API à ${this.baseUrl} (${networkErr.message}). ` +
          `Vérifiez l'URL, que le serveur tourne, et la config CORS.`,
        0,
        null
      );
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

  async _requestMultipart(path, { formData, query, expectBlob = false }) {
    const url = this._buildUrl(path, query);
    const headers = this.apiKey ? { "X-API-Key": this.apiKey } : {};

    let response;
    try {
      response = await fetch(url, { method: "POST", headers, body: formData });
    } catch (networkErr) {
      throw new ApiError(
        `Impossible de joindre l'API (${networkErr.message}).`,
        0,
        null
      );
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

  // -- /models --------------------------------------------------------------

  listModels() {
    return this._request("/models/details");
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

  reloadPredictor(model) {
    return this._request("/predict/reload", {
      method: "POST",
      query: model ? { model } : undefined,
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
}

export default SentimentApiClient;
