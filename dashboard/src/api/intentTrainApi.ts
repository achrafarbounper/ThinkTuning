/**
 * intentTrainApi.ts
 * ---------------------------------------------------------------------
 * Façade « entraînement d'intention » (chat / action) par-dessus le client
 * API unique (SCRUM-95).
 *
 * Même approche que intentApi.ts : on ne duplique PAS le transport
 * (clientCore) — on réutilise les méthodes /train/intent du client partagé
 * et on aligne ici les types et helpers spécifiques à l'intention. Les
 * formes de jobs (IntentTrainJob) vivent dans components/types.ts, comme
 * TrainJob / PipelineJob.
 */

import type { SentimentApiClient } from "./sentimentApiClient";
import type { IntentTrainJob } from "../components/types";

export type { IntentTrainJob };

/** Modèle de base par défaut (identique au défaut du backend IntentTrainRequest). */
export const DEFAULT_INTENT_BASE_MODEL =
  "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2";

/** Chemin du dataset d'intention par défaut (JSONL {"text","label"}). */
export const DEFAULT_INTENT_DATASET = "data/intent_dataset.jsonl";

/** Corps de POST /train/intent (les champs optionnels valent le défaut backend). */
export interface IntentTrainPayload {
  dataset_path: string;
  base_model: string;
  /** Version d'intention source (continual training) ou null = from scratch. */
  base_model_version: string | null;
  epochs: number;
  batch_size: number;
  learning_rate: number;
  max_length: number;
  test_size: number;
  quantize_int8: boolean;
  activate: boolean;
}

/** Réponse de GET /train/intent/versions. */
export interface IntentModelVersions {
  total: number;
  items: string[];
  /** Version résolue par défaut (active.json ou dernière valide) ; null si aucune. */
  active?: string | null;
}

/** Réponse paginée de GET /train/intent/jobs. */
export interface IntentJobsPage {
  total: number;
  items: IntentTrainJob[];
  limit: number;
  offset: number;
}

/** Lance l'entraînement d'intention (202 → job TrainJob kind="intent"). */
export function startIntentTraining(
  client: SentimentApiClient,
  payload: Partial<IntentTrainPayload>
): Promise<IntentTrainJob | null> {
  return client.startIntentTraining(payload) as Promise<IntentTrainJob | null>;
}

/** Statut d'un job d'entraînement d'intention. */
export function getIntentTrainingStatus(
  client: SentimentApiClient,
  jobId: string
): Promise<IntentTrainJob | null> {
  return client.getIntentTrainingStatus(jobId) as Promise<IntentTrainJob | null>;
}

/** Annule un job d'entraînement d'intention. */
export function cancelIntentTraining(
  client: SentimentApiClient,
  jobId: string
): Promise<IntentTrainJob | null> {
  return client.cancelIntentTraining(jobId) as Promise<IntentTrainJob | null>;
}

/** Historique paginé des jobs d'intention (tri started_at DESC). */
export function listIntentTrainingJobs(
  client: SentimentApiClient,
  {
    status,
    limit = 20,
    offset = 0,
  }: { status?: string; limit?: number; offset?: number } = {}
): Promise<IntentJobsPage | null> {
  return client.listIntentTrainingJobs({
    status,
    limit,
    offset,
  }) as Promise<IntentJobsPage | null>;
}

/** Versions d'intention valides + pointeur actif. */
export function getIntentModelVersions(
  client: SentimentApiClient
): Promise<IntentModelVersions | null> {
  return client.getIntentModelVersions() as Promise<IntentModelVersions | null>;
}

/**
 * Active une version d'intention PUIS recharge le classifieur.
 * Chaînage volontaire : le store (active.json) et le runtime (classifieur
 * chargé en mémoire) sont séparés côté backend.
 */
export async function activateIntentVersion(
  client: SentimentApiClient,
  version: string
): Promise<void> {
  await client.activateIntentVersion(version);
  await client.reloadClassifier("intent");
}