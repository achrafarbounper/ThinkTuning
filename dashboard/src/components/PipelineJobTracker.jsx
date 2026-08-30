import { useEffect, useState } from "react";
import { PIPELINE_STEPS } from "../api/sentimentApiClient";

/**
 * PipelineJobTracker — suivi d'un job pipeline end-to-end
 * (labeling -> filtering -> fine-tuning), sur le modèle de TrainJobTracker.
 */

const STEP_LABELS = {
  queued: "En file d'attente",
  labeling: "Labeling DistilBERT",
  filtering: "Filtrage par confidence",
  finetuning: "Fine-tuning LLM (LoRA)",
  done: "Terminé",
  cancelled: "Annulé",
};

function formatDuration(startedAt, endSeconds) {
  if (!startedAt || !endSeconds) return null;
  const seconds = Math.max(0, Math.round(endSeconds - startedAt));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.round(seconds / 60)}m`;
}

export default function PipelineJobTracker({ job, onCancel, cancelLoading = false }) {
  const [nowTs, setNowTs] = useState(0);
  const isRunning = job?.status === "running" || job?.status === "pending";

  useEffect(() => {
    if (!isRunning) return undefined;
    const timer = setInterval(() => setNowTs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [isRunning]);

  if (!job) return null;

  const stepIndex = PIPELINE_STEPS.indexOf(job.step);
  const isCompleted = job.status === "completed";
  const isFailed = job.status === "failed";
  const isCancelled = job.status === "cancelled";

  const endSeconds = job.finished_at ?? (nowTs ? nowTs / 1000 : null);
  const duration = formatDuration(job.started_at, endSeconds);

  const statusColor = {
    running: "#2563eb",
    completed: "#16a34a",
    failed: "#dc2626",
    cancelled: "#ea580c",
    pending: "#6b7280",
  }[job.status] || "#9ca3af";

  const stepLabel = STEP_LABELS[job.step] || job.step;

  return (
    <div className="tt-job-live">
      <div className="tt-job-live-head">
        <h3 className="tt-subtitle">
          Job <span className="tt-mono">{job.job_id.slice(0, 8)}</span>
        </h3>
        <span
          className={`tt-tag tt-tag-status-${job.status}`}
          style={{ color: statusColor }}
        >
          {job.status}
        </span>
      </div>

      <p className="tt-hint">
        Étape actuelle : <strong>{stepLabel}</strong>
        {duration && <> · Durée : {duration}</>}
      </p>

      <ul className="tt-tracker">
        {PIPELINE_STEPS.map((step, index) => {
          const stateClass =
            (isFailed || isCancelled) && index === stepIndex
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
      </ul>

      {job.model_path && (
        <p className="tt-hint">
          {isCompleted ? "Modèle LoRA produit : " : "Dataset labelé : "}
          <span className="tt-mono">{job.model_path}</span>
        </p>
      )}

      {job.error && <p className="tt-hint tt-hint-error tt-job-error">{job.error}</p>}

      {isRunning && (
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
