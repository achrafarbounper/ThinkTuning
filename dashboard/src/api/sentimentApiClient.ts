/**
 * sentimentApiClient.ts
 * ---------------------------------------------------------------------
 * Barrel (point d'entrée) du client API ThinkTuning.
 *
 * Rôle : offrir l'API publique historique à tous les consommateurs tout en
 * hébergeant la classe complète du client, adossée au cœur de transport et
 * aux modules non critiques :
 *
 *   - clientCore.ts      → transport HTTP critique (ApiError, _request, …)
 *   - agentSettings.ts   → helpers assistant IA (settings, storage, /agent)
 *   - jobSteps.ts        → ordres d'étapes (TRAIN_STEPS, PIPELINE_STEPS)
 */

import { SentimentApiClientCore, ApiError } from "./clientCore";

export { SentimentApiClientCore } from "./clientCore";
export {
  DEFAULT_BASE_URL,
  DEFAULT_TIMEOUT_MS,
  DEFAULT_MULTIPART_TIMEOUT_MS,
  ApiError,
} from "./clientCore";

/** Entrée de `/models/details`. */
export interface ModelVersion {
  name: string;
  active?: boolean;
  [key: string]: unknown;
}

/** Réponse de `/health`. */
export interface ApiHealth {
  model_available?: boolean;
  active_jobs?: number;
  [key: string]: unknown;
}

/** Réponse de `/predict` : un résultat par texte. */
export interface PredictionResult {
  text: string;
  sentiment: string;
  confidence: number;
  /** Estampille ajoutée côté front à l'entrée dans l'historique. */
  timestamp?: number;
  [key: string]: unknown;
}

/** Réponse de `/explain`. */
export interface Explanation {
  sentiment?: string;
  confidence?: number;
  explanation?: string;
  [key: string]: unknown;
}

/** Résultat de `/classifiers/{name}/predict` (label + confiance). */
export interface ClassifierPrediction {
  text: string;
  label: string;
  confidence: number;
  /** Distribution complète { label: probabilité } (présente selon le moteur). */
  probabilities?: Record<string, number>;
  [key: string]: unknown;
}

/**
 * Client API complet : étend le transport (clientCore) avec tous les endpoints
 * métier du backend FastAPI. Instancié une fois dans le contexte (AppProvider).
 */
export class SentimentApiClient extends SentimentApiClientCore {
  // -- /health, /metrics (sans authentification) --------------------------

  getHealth(): Promise<ApiHealth | null> {
    return this._request<ApiHealth>("/health");
  }

  /** Exposition Prometheus (texte brut), via le transport central. */
  async getMetricsRaw(): Promise<string> {
    return this._requestText("/metrics");
  }

  /** Endpoint proxy JSON de secours (voir api/routes/metrics.py). */
  async getMetricsJson(): Promise<unknown> {
    return this._request<unknown>("/metrics/json");
  }

  // -- /classifiers (système de classification, Phase 5) -------------------

  /** Liste des classifieurs enregistrés + synthèse de santé (monitoring). */
  listClassifiers() {
    return this._request<{
      classifiers?: Array<Record<string, unknown>>;
      summary?: { total?: number; healthy?: number; status?: string };
    }>("/classifiers");
  }

  /** Instantané d'un classifieur (info, métriques, health, warmup). */
  getClassifier(name: string) {
    return this._request<Record<string, unknown>>(
      `/classifiers/${encodeURIComponent(name)}`
    );
  }

  /** Prédiction via un classifieur (ex. ``intent``), ordre préservé. */
  predictClassifier(
    name: string,
    texts: string[]
  ): Promise<{ results?: ClassifierPrediction[] } | null> {
    return this._request(`/classifiers/${encodeURIComponent(name)}/predict`, {
      method: "POST",
      body: { texts },
    });
  }

  /** Recharge le modèle actif d'un classifieur depuis le disque. */
  reloadClassifier(name: string) {
    return this._request(`/classifiers/${encodeURIComponent(name)}/reload`, {
      method: "POST",
    });
  }

  // -- /models --------------------------------------------------------------

  listModels(): Promise<ModelVersion[] | null> {
    return this._request<ModelVersion[]>("/models/details");
  }

  // -- /evaluate ------------------------------------------------------------

  getConfusion({ model, limit }: { model?: string; limit?: number } = {}) {
    return this._request("/evaluate/confusion", { query: { model, limit } });
  }

  // -- /predict ---------------------------------------------------------------

  predict(texts: string[], model?: string): Promise<{ results?: PredictionResult[] } | null> {
    return this._request("/predict", {
      method: "POST",
      body: { texts },
      query: model ? { model } : undefined,
    });
  }

  predictBatchJson({
    file,
    textColumn = "text",
    model,
  }: { file?: File; textColumn?: string; model?: string } = {}) {
    if (!file) throw new ApiError("Aucun fichier CSV fourni.", 0, null);
    const form = new FormData();
    form.append("file", file);
    form.append("text_column", textColumn);
    form.append("response_format", "json");
    return this._requestMultipart("/predict/batch", {
      formData: form,
      query: model ? { model } : undefined,
    });
  }

  predictBatchCsv({
    file,
    textColumn = "text",
    model,
  }: { file?: File; textColumn?: string; model?: string } = {}) {
    if (!file) throw new ApiError("Aucun fichier CSV fourni.", 0, null);
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

  /** Détection de dérive entre deux batches, via deux fichiers CSV uploadés. */
  driftCsv({
    fileA,
    fileB,
    textColumn = "text",
    threshold,
    method,
    model,
  }: {
    fileA?: File;
    fileB?: File;
    textColumn?: string;
    threshold?: number | string;
    method?: string;
    model?: string;
  } = {}) {
    if (!fileA || !fileB) {
      throw new ApiError("Deux fichiers CSV (A et B) sont requis.", 0, null);
    }
    const form = new FormData();
    form.append("file_a", fileA);
    form.append("file_b", fileB);
    form.append("text_column", textColumn);
    if (threshold !== undefined && threshold !== "") form.append("threshold", String(threshold));
    if (method) form.append("method", method);
    return this._requestMultipart("/drift", {
      formData: form,
      query: model ? { model } : undefined,
    });
  }

  /** Détection de dérive entre deux listes de textes (mode JSON). */
  driftTexts({
    textsA,
    textsB,
    threshold,
    method,
    model,
  }: {
    textsA?: string[];
    textsB?: string[];
    threshold?: number | string;
    method?: string;
    model?: string;
  } = {}) {
    const body: Record<string, unknown> = { texts_a: textsA, texts_b: textsB };
    if (threshold !== undefined && threshold !== "") body.threshold = threshold;
    if (method) body.method = method;
    return this._request("/drift", {
      method: "POST",
      body,
      query: model ? { model } : undefined,
    });
  }

  reloadPredictor(model?: string) {
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
  explain({ text, model }: { text: string; model?: string } = { text: "" }) {
    return this._request<Explanation>("/explain", {
      method: "POST",
      body: model ? { text, model } : { text },
    });
  }

  // -- /train -----------------------------------------------------------------

  startTraining(payload: unknown) {
    return this._request("/train", { method: "POST", body: payload });
  }

  getTrainingStatus(jobId: string) {
    return this._request(`/train/status/${encodeURIComponent(jobId)}`);
  }

  cancelTraining(jobId: string) {
    return this._request(`/train/cancel/${encodeURIComponent(jobId)}`, {
      method: "POST",
    });
  }

  listTrainingJobs({ status, limit, offset }: { status?: string; limit?: number; offset?: number } = {}) {
    return this._request("/train/jobs", { query: { status, limit, offset } });
  }

  /** SCRUM-73 : historique des métriques par epoch d'un job (loss / F1 / accuracy). */
  getTrainingHistory(jobId: string) {
    return this._request(`/train/history/${encodeURIComponent(jobId)}`);
  }

  /**
   * WebSocket GET /train/stream/{job_id} — métriques live pendant un
   * entraînement (loss / F1 epoch par epoch). Retourne l'URL complète à passer
   * à `new WebSocket()` (le jeton passe en query `?token=`, les navigateurs ne
   * pouvant pas poser de header sur un WebSocket).
   */
  getTrainMetricsStreamUrl(jobId: string): string {
    const wsUrl = this.baseUrl.replace(/^http/, "ws").replace(/^https/, "wss");
    const params = new URLSearchParams();
    if (this.apiKey) params.set("token", this.apiKey);
    const qs = params.toString();
    return `${wsUrl}/train/stream/${encodeURIComponent(jobId)}${qs ? `?${qs}` : ""}`;
  }
  // -- /train/schedules (SCRUM-34 : planification récurrente) -----------------

  /** Programme un entraînement récurrent (POST /train/schedule). */
  scheduleTraining(payload: unknown) {
    return this._request("/train/schedule", { method: "POST", body: payload });
  }

  /** Liste les planifications actives : { total, items: ScheduledJob[] }. */
  listSchedules() {
    return this._request("/train/schedules");
  }

  /** Supprime une planification récurrente. */
  deleteSchedule(scheduleId: string) {
    return this._request(`/train/schedules/${encodeURIComponent(scheduleId)}`, {
      method: "DELETE",
    });
  }

  // -- /active_learning & /annotate (SCRUM-55) --------------------------------

  /** Exemples les plus incertains (triés par proximité de la confiance à 1/3). */
  getActiveLearning({
    texts,
    datasetPath,
    topN = 50,
    batchSize = 32,
    modelVersion,
  }: {
    texts?: string[];
    datasetPath?: string;
    topN?: number;
    batchSize?: number;
    modelVersion?: string;
  } = {}) {
    return this._request("/active_learning", {
      method: "POST",
      body: {
        texts: texts && texts.length ? texts : undefined,
        dataset_path: datasetPath || undefined,
        top_n: topN,
        batch_size: batchSize,
        model_version: modelVersion || undefined,
      },
    });
  }

  /** Enregistre une correction manuelle : { text, label, force? }. */
  annotate({ text, label, force = false }: { text: string; label: string; force?: boolean }) {
    return this._request("/annotate", { method: "POST", body: { text, label, force } });
  }

  /** Annotations stockées : { total, items }. */
  listAnnotations({ limit = 100, offset = 0 } = {}) {
    return this._request("/annotate/list", { query: { limit, offset } });
  }

  /** Fusionne les annotations dans le dataset d'entraînement. */
  mergeAnnotations() {
    return this._request("/annotate/merge", { method: "POST" });
  }

  /** Lance le cycle complet (202 → job asynchrone TrainJob). */
  startActiveLearningCycle(payload: unknown = {}) {
    return this._request("/active_learning/cycle", { method: "POST", body: payload });
  }

  /** Statut du job de cycle. */
  getActiveLearningCycleStatus(jobId: string) {
    return this._request(`/active_learning/cycle/status/${encodeURIComponent(jobId)}`);
  }

  /** Active une version de modèle (422 si artefacts invalides). */
  activateModel(name: string) {
    return this._request(`/models/${encodeURIComponent(name)}/activate`, {
      method: "POST",
    });
  }

  /** Pointeur de la version active. */
  getActiveModel() {
    return this._request("/models/active");
  }

  /**
   * Sanity check comportemental d'une version de modèle.
   * En cas de verdict défaillant, l'API renvoie 503 avec un body `detail`
   * { verdict, detail, accuracy, results } : on le retourne comme un rapport
   * plutôt que de lever, pour simplifier l'affichage dans l'IHM.
   */
  async getModelSanity(model?: string) {
    try {
      const report = (await this._request("/health/model-sanity", {
        query: model ? { model_name: model } : undefined,
      })) as Record<string, unknown> | null;
      return { ...report, httpStatus: 200 };
    } catch (err) {
      if (
        err instanceof ApiError &&
        err.status === 503 &&
        typeof err.detail === "object" &&
        err.detail !== null &&
        "verdict" in err.detail
      ) {
        return { ...(err.detail as object), httpStatus: 503 };
      }
      throw err;
    }
  }

  /**
   * Supprime une version de modèle défaillante (DELETE /models/{name}).
   * Refus 409 si version active, 422 si le sanity check est « ok ».
   */
  deleteModel(name: string) {
    return this._request(`/models/${encodeURIComponent(name)}`, { method: "DELETE" });
  }

  // -- /pipeline ---------------------------------------------------------------

  /** Lance le pipeline end-to-end (labeling -> filtering -> fine-tuning LLM). */
  startPipeline(payload: unknown) {
    return this._request("/pipeline", { method: "POST", body: payload });
  }

  getPipelineStatus(jobId: string) {
    return this._request(`/pipeline/status/${encodeURIComponent(jobId)}`);
  }

  cancelPipeline(jobId: string) {
    return this._request(`/pipeline/cancel/${encodeURIComponent(jobId)}`, {
      method: "POST",
    });
  }

  listPipelineJobs({ status, limit, offset }: { status?: string; limit?: number; offset?: number } = {}) {
    return this._request("/pipeline/jobs", { query: { status, limit, offset } });
  }

  // -- /train/intent (entraînement du classifieur d'intention, SCRUM-95) ----

  /** Lance l'entraînement du classifieur d'intention (202 → TrainJob kind="intent"). */
  startIntentTraining(payload: unknown) {
    return this._request("/train/intent", { method: "POST", body: payload });
  }

  /** Statut d'un job d'entraînement d'intention. */
  getIntentTrainingStatus(jobId: string) {
    return this._request(`/train/intent/status/${encodeURIComponent(jobId)}`);
  }

  /** Annule un job d'entraînement d'intention. */
  cancelIntentTraining(jobId: string) {
    return this._request(`/train/intent/cancel/${encodeURIComponent(jobId)}`, {
      method: "POST",
    });
  }

  /** Historique paginé des jobs d'intention uniquement (tri started_at DESC). */
  listIntentTrainingJobs({
    status,
    limit,
    offset,
  }: { status?: string; limit?: number; offset?: number } = {}) {
    return this._request("/train/intent/jobs", { query: { status, limit, offset } });
  }

  /** Versions d'intention valides + pointeur actif : { total, items, active }. */
  getIntentModelVersions() {
    return this._request("/train/intent/versions");
  }

  /** Active une version d'intention (422 si artefacts invalides). */
  activateIntentVersion(version: string) {
    return this._request("/train/intent/activate", {
      method: "POST",
      body: { version },
    });
  }
}

export default SentimentApiClient;

export * from "./agentSettings";
export { TRAIN_STEPS, PIPELINE_STEPS, INTENT_TRAIN_STEPS } from "./jobSteps";
