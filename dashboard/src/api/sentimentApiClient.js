/**
 * sentimentApiClient.js
 * ---------------------------------------------------------------------
 * Barrel (point d'entrée) du client API ThinkTuning.
 *
 * Rôle : offrir l'API publique historique à tous les consommateurs (
 * `import { SentimentApiClient, agentSettingsPayload, ... } from
 * "../api/sentimentApiClient"`) tout en hébergeant la classe complète du
 * client, rapport sur le cœur de transport et les modules non critiques :
 *
 *   - clientCore.js      → transport HTTP critique (ApiError, _request, …)
 *   - agentSettings.js   → helpers assistant IA (settings, storage, /agent)
 *   - jobSteps.js        → ordres d'étapes (TRAIN_STEPS, PIPELINE_STEPS)
 *
 * La classe `SentimentApiClient` étend le cœur avec tous les endpoints métier.
 * Les re-exports ci-dessous garantissent la rétro-compatibilité intégrale des
 * imports existants.
 */

import { SentimentApiClientCore, ApiError } from "./clientCore";

export { SentimentApiClientCore } from "./clientCore";
export {
  DEFAULT_BASE_URL,
  DEFAULT_TIMEOUT_MS,
  DEFAULT_MULTIPART_TIMEOUT_MS,
  ApiError,
} from "./clientCore";
/**
 * Client API complet : étend le transport (clientCore) avec tous les endpoints
 * métier du backend FastAPI. Instancié une fois dans le contexte (AppProvider).
 */
export class SentimentApiClient extends SentimentApiClientCore {
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

  /** SCRUM-73 : historique des métriques par epoch d'un job (loss / F1 / accuracy). */
  getTrainingHistory(jobId) {
    return this._request(`/train/history/${encodeURIComponent(jobId)}`);
  }

  /**
   * WebSocket GET /train/stream/{job_id} — métriques live pendant un
   * entraînement (loss / F1 epoch par epoch). Usage interne au dashboard :
   * le jeton (X-API-Key ou DASHBOARD_WS_TOKEN côté serveur) est passé en
   * query param `?token=` car les navigateurs ne peuvent pas poser de header
   * sur un WebSocket. Retourne l'URL complète à passer à `new WebSocket()`.
   */
  getTrainMetricsStreamUrl(jobId) {
    const wsUrl = this.baseUrl
      .replace(/^http/, "ws")
      .replace(/^https/, "wss");
    const params = new URLSearchParams();
    if (this.apiKey) params.set("token", this.apiKey);
    const qs = params.toString();
    return `${wsUrl}/train/stream/${encodeURIComponent(jobId)}${qs ? `?${qs}` : ""}`;
  }

  // -- /train/schedules (SCRUM-34 : planification récurrente) -----------------

  /**
   * Programme un entraînement récurrent (POST /train/schedule).
   * Payload : { train: {...}, cron?: "0 2 * * *", interval_minutes?: 60 }
   * Retourne un ScheduledJob (schedule_id, next_run_at, trigger, ...).
   */
  scheduleTraining(payload) {
    return this._request("/train/schedule", { method: "POST", body: payload });
  }

  /** Liste les planifications actives : { total, items: ScheduledJob[] }. */
  listSchedules() {
    return this._request("/train/schedules");
  }

  /** Supprime une planification récurrente. */
  deleteSchedule(scheduleId) {
    return this._request(`/train/schedules/${encodeURIComponent(scheduleId)}`, {
      method: "DELETE",
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

export * from "./agentSettings";
export { TRAIN_STEPS, PIPELINE_STEPS } from "./jobSteps";