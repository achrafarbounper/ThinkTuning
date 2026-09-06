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

  /**
   * Santé de l'API — MIGRÉ vers la surface v1 (découplage frontend/backend).
   *
   * Strangler pattern : GET /api/v1/health expose le MÊME shape que /health
   * legacy (contrat verrouillé par tests de non-régression backend), donc le
   * polling AppProvider n'a besoin d'aucune adaptation. La surface legacy
   * /health reste servie tant que d'autres endpoints ne sont pas migrés.
   */
  getHealth(): Promise<ApiHealth | null> {
    return this._request<ApiHealth>("/api/v1/health");
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

  /**
   * Prédiction de sentiment — MIGRÉ vers la surface v1 (découplage).
   * Différences v1 : la version de modèle passe du query (?model=) au CORPS
   * (model_name) ; la réponse ajoute model_version (additif, transparent).
   * Auth inchangée : X-API-Key requise, comme le legacy.
   */
  predict(
    texts: string[],
    model?: string
  ): Promise<{ results?: PredictionResult[]; model_version?: string } | null> {
    return this._request("/api/v1/predict", {
      method: "POST",
      body: { texts, model_name: model || undefined },
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

  /**
   * Rechargement du predictor actif — MIGRÉ vers la surface v1.
   * Aucun paramètre : les deux surfaces ne rechargent QUE la version active
   * (le ``?model=`` historique était ignoré par le backend legacy). En cas de
   * refus SCRUM-74 (modèle non sain), l'API répond 503 ``model_unhealthy`` et
   * le transport remonte ``error.message`` comme message lisible.
   * Signature conservée (paramètre ignoré) pour ne pas casser l'appelant.
   */
  reloadPredictor(_model?: string) {
    return this._request("/api/v1/predict/reload", { method: "POST" });
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

  // -- /train (MIGRÉ vers la surface v1 — Phase 3d, découplage) ----------------

  /**
   * Démarre un entraînement — MIGRÉ vers POST /api/v1/train (shape identique,
   * contrat verrouillé par tests backend test_api_v1_contract.py).
   */
  startTraining(payload: unknown) {
    return this._request("/api/v1/train", { method: "POST", body: payload });
  }

  /** Statut d'un job — MIGRÉ vers GET /api/v1/train/status/{job_id}. */
  getTrainingStatus(jobId: string) {
    return this._request(`/api/v1/train/status/${encodeURIComponent(jobId)}`);
  }

  /** Annule un job — MIGRÉ vers POST /api/v1/train/cancel/{job_id}. */
  cancelTraining(jobId: string) {
    return this._request(`/api/v1/train/cancel/${encodeURIComponent(jobId)}`, {
      method: "POST",
    });
  }

  /** Liste paginée des jobs — MIGRÉ vers GET /api/v1/train/jobs. */
  listTrainingJobs({ status, limit, offset }: { status?: string; limit?: number; offset?: number } = {}) {
    return this._request("/api/v1/train/jobs", { query: { status, limit, offset } });
  }

  /**
   * SCRUM-73 : historique des métriques par epoch d'un job (loss / F1 /
   * accuracy) — MIGRÉ vers GET /api/v1/train/history/{job_id}.
   */
  getTrainingHistory(jobId: string) {
    return this._request(`/api/v1/train/history/${encodeURIComponent(jobId)}`);
  }

  /**
   * WebSocket /api/v1/train/stream/{job_id} — MIGRÉ vers la surface v1
   * (métriques live pendant un entraînement, loss / F1 epoch par epoch).
   * Retourne l'URL complète à passer à `new WebSocket()` (le jeton passe en
   * query `?token=`, les navigateurs ne pouvant pas poser de header sur un
   * WebSocket).
   */
  getTrainMetricsStreamUrl(jobId: string): string {
    const wsUrl = this.baseUrl.replace(/^http/, "ws").replace(/^https/, "wss");
    const params = new URLSearchParams();
    if (this.apiKey) params.set("token", this.apiKey);
    const qs = params.toString();
    return `${wsUrl}/api/v1/train/stream/${encodeURIComponent(jobId)}${qs ? `?${qs}` : ""}`;
  }
  // -- /train/schedules (SCRUM-34 : planification récurrente — MIGRÉ v1) ------

  /** Programme un entraînement récurrent — MIGRÉ vers POST /api/v1/train/schedule. */
  scheduleTraining(payload: unknown) {
    return this._request("/api/v1/train/schedule", { method: "POST", body: payload });
  }

  /** Liste les planifications actives — MIGRÉ vers GET /api/v1/train/schedules. */
  listSchedules() {
    return this._request("/api/v1/train/schedules");
  }

  /** Supprime une planification — MIGRÉ vers DELETE /api/v1/train/schedules/{id}. */
  deleteSchedule(scheduleId: string) {
    return this._request(`/api/v1/train/schedules/${encodeURIComponent(scheduleId)}`, {
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
   * Sanity check comportemental d'une version de modèle — MIGRÉ vers v1
   * (découplage frontend/backend).
   *
   * En cas de verdict défaillant, l'API répond 503 sans lever pour l'IHM.
   * Deux enveloppes sont normalisées (déploiements mixtes pendant la
   * migration) :
   *  - v1 domaine : {"error": {code, message, details:{verdict, accuracy,
   *    results, ...}}} — le message métier vit dans error.message ;
   *  - legacy FastAPI : {"detail": {verdict, detail, accuracy, results}}.
   * Le rapport retourné garde le shape attendu par ModelSanityPanel.
   */
  async getModelSanity(model?: string) {
    try {
      const report = (await this._request("/api/v1/health/model-sanity", {
        query: model ? { model_name: model } : undefined,
      })) as Record<string, unknown> | null;
      return { ...report, httpStatus: 200 };
    } catch (err) {
      if (!(err instanceof ApiError) || err.status !== 503) throw err;

      // Enveloppe v1 : rapport = error.details, detail = error.message.
      const detailObj =
        err.detail !== null && typeof err.detail === "object"
          ? (err.detail as Record<string, unknown>)
          : null;
      const v1Error =
        detailObj && "error" in detailObj
          ? (detailObj.error as
              | { message?: unknown; details?: Record<string, unknown> }
              | undefined)
          : undefined;
      if (
        v1Error &&
        typeof v1Error === "object" &&
        v1Error.details &&
        typeof v1Error.details === "object" &&
        "verdict" in v1Error.details
      ) {
        return {
          ...(v1Error.details as object),
          detail:
            typeof v1Error.message === "string"
              ? v1Error.message
              : ((v1Error.details as { detail?: string }).detail ?? ""),
          httpStatus: 503,
        };
      }

      // Enveloppe legacy (backend antérieur à la v1, déploiement mixte).
      if (
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

  // -- /train/intent (SCRUM-95 : classifieur d'intention — MIGRÉ v1) ----------

  /**
   * Lance l'entraînement du classifieur d'intention — MIGRÉ vers
   * POST /api/v1/train/intent (202 → TrainJob kind="intent" ; 422 enveloppe
   * domaine si dataset/version source invalides).
   */
  startIntentTraining(payload: unknown) {
    return this._request("/api/v1/train/intent", { method: "POST", body: payload });
  }

  /** Statut d'un job d'intention — MIGRÉ vers GET /api/v1/train/intent/status/{job_id}. */
  getIntentTrainingStatus(jobId: string) {
    return this._request(`/api/v1/train/intent/status/${encodeURIComponent(jobId)}`);
  }

  /** Annule un job d'intention — MIGRÉ vers POST /api/v1/train/intent/cancel/{job_id}. */
  cancelIntentTraining(jobId: string) {
    return this._request(`/api/v1/train/intent/cancel/${encodeURIComponent(jobId)}`, {
      method: "POST",
    });
  }

  /**
   * Historique paginé des jobs d'intention uniquement (tri started_at DESC) —
   * MIGRÉ vers GET /api/v1/train/intent/jobs.
   */
  listIntentTrainingJobs({
    status,
    limit,
    offset,
  }: { status?: string; limit?: number; offset?: number } = {}) {
    return this._request("/api/v1/train/intent/jobs", { query: { status, limit, offset } });
  }

  /**
   * Versions d'intention valides + pointeur actif : { total, items, active } —
   * MIGRÉ vers GET /api/v1/train/intent/versions.
   */
  getIntentModelVersions() {
    return this._request("/api/v1/train/intent/versions");
  }

  /**
   * Active une version d'intention (422 si artefacts invalides) — MIGRÉ vers
   * POST /api/v1/train/intent/activate.
   */
  activateIntentVersion(version: string) {
    return this._request("/api/v1/train/intent/activate", {
      method: "POST",
      body: { version },
    });
  }
}

export default SentimentApiClient;

export * from "./agentSettings";
export { TRAIN_STEPS, PIPELINE_STEPS, INTENT_TRAIN_STEPS } from "./jobSteps";
