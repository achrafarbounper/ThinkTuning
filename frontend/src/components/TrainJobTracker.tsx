import { useEffect, useState } from "react";
import { TRAIN_STEPS } from "../api/sentimentApiClient";
import type { TrainJobTrackerProps } from "./types";

/**
 * TrainJobTracker — affiche le suivi d'un job d'entraînement en cours :
 * statut, étape courante, timing, et possibilité d'annulation.
 */

const STEP_LABELS: Record<string, string> = {
  queued: "En file d'attente",
  loading_dataset: "Chargement du dataset",
  splitting_dataset: "Split train/val",
  augmenting_dataset: "Recomposition (EDA)",
  building_dataloaders: "Construction DataLoaders",
  computing_class_weights: "Poids des classes",
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

export default function TrainJobTracker({
  job,
  onCancel,
  cancelLoading = false,
}: TrainJobTrackerProps) {
  const [nowTs, setNowTs] = useState(0);
  const isRunning = job?.status === "running" || job?.status === "pending";

  useEffect(() => {
    if (!isRunning) return undefined;
    const timer = setInterval(() => setNowTs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [isRunning]);

  if (!job) return null;

  const stepIndex = TRAIN_STEPS.indexOf(job.step ?? "");
  const isCompleted = job.status === "completed";
  const isFailed = job.status === "failed";
  const isCancelled = job.status === "cancelled";

  const endSeconds = job.finished_at ?? (nowTs ? nowTs / 1000 : null);
  const duration = formatDuration(job.started_at, endSeconds);

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
          Job <span className="tt-mono">{job.job_id.slice(0, 8)}</span>
        </h3>
        <span
          className={`tt-tag tt-tag-status-${job.status}`}
          style={{ color }}
        >
          {job.status}
        </span>
      </div>

      <p className="tt-hint">
        Étape actuelle : <strong>{stepLabel}</strong>
        {duration && <> · Durée : {duration}</>}
      </p>

      <ul className="tt-tracker">
        {TRAIN_STEPS.map((step, index) => {
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

      {job.regression && (
        <p className="tt-hint tt-hint-error tt-job-error">
          ⚠ Régression détectée{job.regression_detail ? ` — ${job.regression_detail}` : ""}
        </p>
      )}

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
