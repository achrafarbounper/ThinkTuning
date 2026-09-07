import { useEffect, useState } from "react";
import { INTENT_TRAIN_STEPS } from "../api/sentimentApiClient";
import type { IntentTrainJobTrackerProps } from "./types";

/**
 * IntentTrainJobTracker — suivi d'un job d'entraînement d'intention
 * (chat / action) : statut, étape courante, pourcentage global (job.progress
 * alimenté par le runner backend), timing et annulation. Même structure
 * visuelle que TrainJobTracker, avec la liste d'étapes réduite du pipeline
 * d'intention (INTENT_TRAIN_STEPS).
 */

const STEP_LABELS: Record<string, string> = {
  queued: "En file d'attente",
  loading_dataset: "Chargement du dataset",
  splitting_dataset: "Split train/val",
  loading_model: "Chargement du modèle",
  training: "Entraînement",
  saving_model: "Sauvegarde du modèle",
  done: "Terminé",
  cancelled: "Annulé",
};

function formatDuration(startedAt?: number, endSeconds?: number | null): string | null {
  if (!startedAt || !endSeconds) return null;
  const seconds = Math.max(0, Math.round(endSeconds - startedAt));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.round(seconds / 60)}m`;
}

export default function IntentTrainJobTracker({
  job,
  onCancel,
  cancelLoading = false,
}: IntentTrainJobTrackerProps) {
  const [nowTs, setNowTs] = useState(0);
  const isRunning = job?.status === "running" || job?.status === "pending";

  useEffect(() => {
    if (!isRunning) return undefined;
    const timer = setInterval(() => setNowTs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [isRunning]);

  if (!job) return null;

  const stepIndex = INTENT_TRAIN_STEPS.indexOf(job.step ?? "");
  const isCompleted = job.status === "completed";
  const isFailed = job.status === "failed";
  const isCancelled = job.status === "cancelled";

  const endSeconds = job.finished_at ?? (nowTs ? nowTs / 1000 : null);
  const duration = formatDuration(job.started_at, endSeconds);

  const globalPct =
    typeof job.progress?.global_pct === "number" ? job.progress.global_pct : null;

  const statusColor: Record<string, string> = {
    running: "#2563eb",
    completed: "#16a34a",
    failed: "#dc2626",
    cancelled: "#ea580c",
    pending: "#6b7280",
  };
  const color = statusColor[job.status] || "#9ca3af";

  const stepLabel = STEP_LABELS[job.step ?? ""] || job.step;

  return (
    <div className="tt-job-live">
      <div className="tt-job-live-head">
        <h3 className="tt-subtitle">
          Job intention <span className="tt-mono">{job.job_id.slice(0, 8)}</span>
        </h3>
        <span className={`tt-tag tt-tag-status-${job.status}`} style={{ color }}>
          {job.status}
        </span>
      </div>

      <p className="tt-hint">
        Étape actuelle : <strong>{stepLabel}</strong>
        {duration && <> · Durée : {duration}</>}
        {globalPct !== null && <> · Avancement : {Math.round(globalPct)} %</>}
      </p>

      {globalPct !== null && (
        <div
          role="progressbar"
          aria-valuenow={Math.round(globalPct)}
          aria-valuemin={0}
          aria-valuemax={100}
          style={{
            height: 6,
            borderRadius: 3,
            background: "#e5e7eb",
            overflow: "hidden",
            marginBottom: 8,
          }}
        >
          <div
            style={{
              width: `${Math.min(100, Math.max(0, globalPct))}%`,
              height: "100%",
              background: isFailed ? "#dc2626" : "#2563eb",
              transition: "width 0.4s ease",
            }}
          />
        </div>
      )}

      <ul className="tt-tracker">
        {INTENT_TRAIN_STEPS.map((step, index) => {
          const stateClass =
            isFailed && index === stepIndex
              ? "tt-tracker-error"
              : index < stepIndex || isCompleted
              ? "tt-tracker-done"
              : index === stepIndex
              ? "tt-tracker-active"
              : "";
          return (
            <li key={step} className={`tt-tracker-step ${stateClass}`.trim()}>
              <span className="tt-tracker-dot" aria-hidden="true" />
              {STEP_LABELS[step]}
            </li>
          );
        })}
        {isCancelled && (
          <li className="tt-tracker-step tt-tracker-error">
            <span className="tt-tracker-dot" aria-hidden="true" />
            Annulé
          </li>
        )}
      </ul>

      {job.error && <p className="tt-hint tt-hint-error tt-job-error">{job.error}</p>}

      {(job.status === "running" || job.status === "pending") && (
        <button
          className="tt-btn tt-btn-danger"
          type="button"
          onClick={onCancel}
          disabled={cancelLoading}
        >
          Annuler ce job
        </button>
      )}
    </div>
  );
}