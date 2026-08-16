import React, { useMemo } from "react";
import { TRAIN_STEPS } from "../sentimentApiClient";

/**
 * TrainJobTracker — affiche le suivi d'un job d'entraînement en cours
 * avec visualisation de l'étape courant, statut, timing, et possibilité d'annulation.
 */
export default function TrainJobTracker({
  job,
  onCancel,
  cancelLoading = false,
}) {
  if (!job) return null;

  const stepIndex = TRAIN_STEPS.indexOf(job.step);
  const progress = stepIndex === -1 ? 0 : ((stepIndex + 1) / TRAIN_STEPS.length) * 100;

  const isRunning = job.status === "running";
  const isCompleted = job.status === "completed";
  const isFailed = job.status === "failed";
  const isCancelled = job.status === "cancelled";
  const isDone = isCompleted || isFailed || isCancelled;

  const duration = useMemo(() => {
    if (!job.started_at) return null;
    const end = job.finished_at || Date.now() / 1000;
    const seconds = Math.round(end - job.started_at);
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.round(seconds / 60);
    return `${minutes}m`;
  }, [job.started_at, job.finished_at]);

  const statusColor = {
    running: "#2563eb",
    completed: "#16a34a",
    failed: "#dc2626",
    cancelled: "#ea580c",
    pending: "#6b7280",
  }[job.status] || "#9ca3af";

  const stepLabel = {
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
  }[job.step] || job.step;

  return (
    
    <div className="tt-tracker">
      <div className="tt-tracker-header">
        <div className="tt-tracker-info">
          <h3 className="tt-tracker-title">
            Suivi du job
            <span className="tt-tracker-id">{job.job_id.slice(0, 8)}</span>
          </h3>
          <div className="tt-tracker-meta">
            <span
              className="tt-tracker-status"
              style={{
                backgroundColor: statusColor,
                color: "#fff",
                padding: "0.25rem 0.75rem",
                borderRadius: "4px",
                fontSize: "0.85rem",
                fontWeight: "500",
              }}
            >
              {job.status.toUpperCase()}
            </span>
            {duration && (
              <span className="tt-tracker-duration">
                ⏱ {duration}
              </span>
            )}
          </div>
        </div>
        {isRunning && (
          <button
            className="tt-btn tt-btn-danger"
            onClick={onCancel}
            disabled={cancelLoading}
            title="Annuler cet entraînement"
          >
            {cancelLoading ? "Annulation…" : "Annuler"}
          </button>
        )}
      </div>

      {/* --- Progression visuelle ------------------------------------------ */}
      <div className="tt-tracker-progress">
        <div className="tt-progress-bar">
          <div
            className="tt-progress-fill"
            style={{
              width: `${progress}%`,
              backgroundColor: statusColor,
              transition: "width 0.3s ease-out",
            }}
          />
        </div>
        <span className="tt-progress-text">
          {stepIndex + 1} / {TRAIN_STEPS.length}
        </span>
      </div>

      {/* --- Étape courante ------------------------------------------------ */}
      <div className="tt-tracker-step">
        <span className="tt-step-label">Étape courante:</span>
        <span className="tt-step-value">{stepLabel}</span>
        {isRunning && <span className="tt-spinner" />}
      </div>

      {/* --- Timeline des étapes -------------------------------------------- */}
      <div className="tt-steps-timeline">
        {TRAIN_STEPS.map((step, idx) => {
          const isActive = step === job.step;
          const isDone = idx < stepIndex;
          const stepName = {
            queued: "En file",
            loading_dataset: "Dataset",
            splitting_dataset: "Split",
            augmenting_dataset: "EDA",
            building_dataloaders: "DataLoaders",
            computing_class_weights: "Poids",
            loading_model: "Modèle",
            training: "Train",
            saving_model: "Sauvegarde",
            done: "✓",
            cancelled: "✕",
          }[step] || step;

          return (
            <div
              key={step}
              className={`tt-step-dot ${isDone ? "tt-step-done" : ""} ${
                isActive ? "tt-step-active" : ""
              }`}
              title={step}
            >
              <span className="tt-step-dot-inner">{stepName}</span>
              {idx < TRAIN_STEPS.length - 1 && (
                <span
                  className={`tt-step-line ${isDone || isActive ? "tt-step-line-done" : ""}`}
                />
              )}
            </div>
          );
        })}
      </div>

      {/* --- Erreur ou message de succès ------------------------------------ */}
      {isFailed && job.error && (
        <div className="tt-tracker-error">
          <strong>Erreur:</strong>
          <pre className="tt-error-text">{job.error}</pre>
        </div>
      )}

      {isCompleted && job.model_path && (
        <div className="tt-tracker-success">
          ✓ Modèle entraîné avec succès:<br />
          <span className="tt-mono">{job.model_path}</span>
        </div>
      )}

      {isCancelled && (
        <div className="tt-tracker-cancelled">
          Entraînement annulé par l'utilisateur
        </div>
      )}
    </div>
  );
}
