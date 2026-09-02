/**
 * types.ts
 * ---------------------------------------------------------------------
 * Types partagés des composants du dashboard.
 */

import type { SentimentApiClient } from "../api/sentimentApiClient";

// --- Confusion & Evaluation -----------------------------------------------

export interface ConfusionPair {
  true_label: string;
  pred_label: string;
  count: number;
}

export interface ConfusionMetrics {
  accuracy?: number;
  f1_macro?: number;
  per_class_recall?: Record<string, number>;
  extras?: Record<string, number | Record<string, number>>;
}

export interface ConfusionData {
  labels?: string[];
  matrix?: number[][];
  metrics?: ConfusionMetrics;
  confusion_pairs?: ConfusionPair[];
  mistakes?: Mistake[];
  n?: number;
  model?: string;
  [key: string]: unknown;
}

export interface ConfusionDataStrict {
  labels?: string[];
  matrix?: number[][];
  metrics?: ConfusionMetrics;
  confusion_pairs?: ConfusionPair[];
  mistakes?: Mistake[];
  n?: number;
  model?: string;
}

export interface Mistake {
  text?: string;
  true_label?: string;
  pred_label?: string;
  confidence?: number;
  [key: string]: unknown;
}

// --- Jobs ------------------------------------------------------------------

export type JobStatus =
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "pending";

export interface TrainJob {
  job_id: string;
  step?: string;
  status: JobStatus | string;
  started_at?: number;
  finished_at?: number;
  model_path?: string;
  error?: string;
  regression?: boolean;
  regression_detail?: string;
  extras?: Record<string, unknown>;
}

export interface PipelineJob {
  job_id: string;
  step?: string;
  status: JobStatus | string;
  started_at?: number;
  finished_at?: number;
  model_path?: string;
  error?: string;
  extras?: Record<string, unknown>;
}

// --- Logs ------------------------------------------------------------------

export interface LogEntry {
  seq?: number;
  ts?: number;
  level?: string;
  step?: string;
  message?: string;
  extras?: Record<string, unknown>;
}

// --- Epochs (métriques en direct) -----------------------------------------

export interface EpochData {
  epoch: number;
  loss?: number;
  f1_macro?: number;
  accuracy?: number;
  extras?: Record<string, unknown>;
}

// --- Training history ------------------------------------------------------

export interface TrainingEpoch {
  epoch: number;
  loss?: number;
  f1_macro?: number;
  extras?: Record<string, unknown>;
}

// --- Sanity check ----------------------------------------------------------

export type SanityVerdict =
  | "ok"
  | "untrained"
  | "fallback_base_model"
  | "model_unavailable"
  | "unknown"
  | "error";

export interface SanityResult {
  text: string;
  expected: string;
  predicted: string;
  correct: boolean;
  confidence: number;
  lang?: string;
}

export interface SanityReport {
  verdict?: SanityVerdict | string;
  detail?: string;
  accuracy?: number;
  results?: SanityResult[];
  httpStatus?: number;
  extras?: Record<string, unknown>;
}

// --- Composants : props partagées -----------------------------------------

export interface StepLogConsoleProps {
  logs: LogEntry[];
  stepLabels?: Record<string, string>;
  currentStep?: string | null;
}

export interface TrainJobTrackerProps {
  job: TrainJob | null | undefined;
  onCancel: () => void;
  cancelLoading?: boolean;
}

export interface PipelineJobTrackerProps {
  job: PipelineJob | null | undefined;
  onCancel: () => void;
  cancelLoading?: boolean;
}

export interface ModelSanityPanelProps {
  client: SentimentApiClient;
  models: Array<{ name: string; path?: string; active?: boolean; created_at?: string }>;
  onModelsChanged?: () => void;
  pushLog?: (level: string, message: string) => void;
}

export interface ModelComparisonPanelProps {
  client: SentimentApiClient;
  modelA?: string;
  modelB?: string;
}

export interface TrainingHistoryChartProps {
  jobs: TrainJob[];
  client: SentimentApiClient;
  pushLog?: (level: string, message: string) => void;
}

export interface TrainMetricsStreamProps {
  jobId: string;
}
