/**
 * Page « Entraînement » — fine-tuning du modèle de sentiments.
 *
 * Regroupe :
 * - le formulaire de lancement (POST /train) avec options avancées repliables,
 * - le suivi temps réel du job en cours (poll 4 s) via TrainJobTracker,
 * - l'historique des jobs (GET /train/jobs) avec filtre par statut et pagination.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "../context/useApp";
import TrainJobTracker from "../components/TrainJobTracker";

const JOBS_POLL_MS = 15000;
const TRAIN_POLL_MS = 4000;

const STEP_LABELS = {
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

function numOrUndef(value) {
  if (value === "" || value === null || value === undefined) return undefined;
  const n = Number(value);
  return Number.isNaN(n) ? undefined : n;
}

function formatEpoch(seconds) {
  if (!seconds) return "—";
  try {
    return new Date(seconds * 1000).toLocaleString();
  } catch {
    return "—";
  }
}

export default function TrainingPage() {
  const { client, refreshModels, pushLog } = useApp();

  const [trainForm, setTrainForm] = useState({
    max_per_lang: 500,
    augment_fraction: 0.4,
    variants_per_example: 2,
    device: "auto",
    epochs: "",
    batch_size: "",
    num_workers: "",
    max_length: "",
    learning_rate: "",
    weight_decay: "",
    warmup_ratio: "",
  });
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [currentJob, setCurrentJob] = useState(null);
  const [trainLoading, setTrainLoading] = useState(false);
  const [trainError, setTrainError] = useState(null);
  const [jobs, setJobs] = useState([]);
  // Pagination / filtrage GET /train/jobs (?status=&limit=&offset=)
  const [jobsStatusFilter, setJobsStatusFilter] = useState("");
  const [jobsLimit] = useState(20);
  const [jobsOffset, setJobsOffset] = useState(0);
  const [jobsTotal, setJobsTotal] = useState(0);

  const trainPollRef = useRef(null);

  // -- Historique des jobs -----------------------------------------------------
  const refreshJobs = useCallback(async () => {
    try {
      // Réponse : { total, items, limit, offset } trié par started_at DESC.
      const result = await client.listTrainingJobs({
        status: jobsStatusFilter || undefined,
        limit: jobsLimit,
        offset: jobsOffset,
      });
      setJobs(result.items);
      setJobsTotal(result.total);
    } catch (err) {
      pushLog("error", `Historique des jobs indisponible : ${err.message}`);
    }
  }, [client, pushLog, jobsStatusFilter, jobsLimit, jobsOffset]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        // Réponse : { total, items, limit, offset } trié par started_at DESC.
        const result = await client.listTrainingJobs({
          status: jobsStatusFilter || undefined,
          limit: jobsLimit,
          offset: jobsOffset,
        });
        if (!cancelled) {
          setJobs(result.items);
          setJobsTotal(result.total);
        }
      } catch (err) {
        if (!cancelled) {
          pushLog("error", `Historique des jobs indisponible : ${err.message}`);
        }
      }
    };
    load();
    const interval = setInterval(load, JOBS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [client, pushLog, jobsStatusFilter, jobsLimit, jobsOffset]);

  // -- Nettoyage du polling d'entraînement au démontage ------------------------
  useEffect(() => {
    return () => {
      if (trainPollRef.current) clearInterval(trainPollRef.current);
    };
  }, []);

  const stopTrainPolling = useCallback(() => {
    if (trainPollRef.current) {
      clearInterval(trainPollRef.current);
      trainPollRef.current = null;
    }
  }, []);

  const pollJob = useCallback(
    (jobId) => {
      stopTrainPolling();
      trainPollRef.current = setInterval(async () => {
        try {
          const job = await client.getTrainingStatus(jobId);
          setCurrentJob(job);
          if (["completed", "failed", "cancelled"].includes(job.status)) {
            stopTrainPolling();
            pushLog(
              job.status === "completed" ? "success" : "warning",
              `Job ${jobId.slice(0, 8)} → ${job.status}${job.error ? ` (${job.error.split("\n")[0]})` : ""}`
            );
            refreshModels();
            refreshJobs();
          }
        } catch (err) {
          pushLog("error", `Suivi du job interrompu : ${err.message}`);
          stopTrainPolling();
        }
      }, TRAIN_POLL_MS);
    },
    [client, stopTrainPolling, pushLog, refreshModels, refreshJobs]
  );

  // -- Actions -------------------------------------------------------------------
  const handleStartTraining = async (e) => {
    e.preventDefault();
    setTrainError(null);
    setTrainLoading(true);
    const payload = {
      max_per_lang: numOrUndef(trainForm.max_per_lang) ?? 500,
      augment_fraction: numOrUndef(trainForm.augment_fraction) ?? 0.4,
      variants_per_example: numOrUndef(trainForm.variants_per_example) ?? 2,
      epochs: numOrUndef(trainForm.epochs),
      batch_size: numOrUndef(trainForm.batch_size),
      num_workers: numOrUndef(trainForm.num_workers),
      max_length: numOrUndef(trainForm.max_length),
      learning_rate: numOrUndef(trainForm.learning_rate),
      weight_decay: numOrUndef(trainForm.weight_decay),
      warmup_ratio: numOrUndef(trainForm.warmup_ratio),
      device: trainForm.device || "auto",
    };
    try {
      const job = await client.startTraining(payload);
      setCurrentJob(job);
      pushLog("info", `Entraînement lancé — job ${job.job_id.slice(0, 8)}`);
      pollJob(job.job_id);
      refreshJobs();
    } catch (err) {
      setTrainError(err.message);
    } finally {
      setTrainLoading(false);
    }
  };

  const handleCancelTraining = async () => {
    if (!currentJob) return;
    try {
      const job = await client.cancelTraining(currentJob.job_id);
      setCurrentJob(job);
      pushLog("warning", `Annulation demandée pour ${job.job_id.slice(0, 8)}`);
    } catch (err) {
      pushLog("error", `Échec de l'annulation : ${err.message}`);
    }
  };

  const handleJobsStatusFilterChange = (event) => {
    setJobsStatusFilter(event.target.value);
    setJobsOffset(0); // repart à la première page quand le filtre change
  };

  return (
    <>
      <header className="page-head">
        <h1>Entraînement</h1>
        <p>Fine-tuning multilingue (FR/EN) avec recomposition de données (EDA).</p>
      </header>

      <div className="page-body">
        <section className="tt-panel">
          <div className="tt-panel-head">
            <h2>Lancer un entraînement</h2>
            <button className="tt-btn tt-btn-ghost" onClick={refreshJobs} type="button">
              Rafraîchir l'historique
            </button>
          </div>

          <form onSubmit={handleStartTraining} className="tt-form tt-train-form">
            <div className="tt-field-grid">
              <label>
                max_per_lang
                <input
                  type="number"
                  value={trainForm.max_per_lang}
                  onChange={(e) => setTrainForm((f) => ({ ...f, max_per_lang: e.target.value }))}
                />
              </label>
              <label>
                augment_fraction
                <input
                  type="number"
                  step="0.05"
                  value={trainForm.augment_fraction}
                  onChange={(e) => setTrainForm((f) => ({ ...f, augment_fraction: e.target.value }))}
                />
              </label>
              <label>
                variants_per_example
                <input
                  type="number"
                  value={trainForm.variants_per_example}
                  onChange={(e) => setTrainForm((f) => ({ ...f, variants_per_example: e.target.value }))}
                />
              </label>
              <label>
                device
                <select
                  value={trainForm.device}
                  onChange={(e) => setTrainForm((f) => ({ ...f, device: e.target.value }))}
                >
                  <option value="auto">auto</option>
                  <option value="cpu">cpu</option>
                  <option value="cuda">cuda</option>
                </select>
              </label>
            </div>

            <details
              className="tt-advanced"
              open={advancedOpen}
              onToggle={(e) => setAdvancedOpen(e.target.open)}
            >
              <summary className="tt-advanced-toggle">Options avancées</summary>
              <div className="tt-field-grid">
                <label>
                  epochs
                  <input type="number" value={trainForm.epochs} onChange={(e) => setTrainForm((f) => ({ ...f, epochs: e.target.value }))} />
                </label>
                <label>
                  batch_size
                  <input type="number" value={trainForm.batch_size} onChange={(e) => setTrainForm((f) => ({ ...f, batch_size: e.target.value }))} />
                </label>
                <label>
                  num_workers
                  <input type="number" value={trainForm.num_workers} onChange={(e) => setTrainForm((f) => ({ ...f, num_workers: e.target.value }))} />
                </label>
                <label>
                  max_length
                  <input type="number" value={trainForm.max_length} onChange={(e) => setTrainForm((f) => ({ ...f, max_length: e.target.value }))} />
                </label>
                <label>
                  learning_rate
                  <input type="number" step="0.00001" value={trainForm.learning_rate} onChange={(e) => setTrainForm((f) => ({ ...f, learning_rate: e.target.value }))} />
                </label>
                <label>
                  weight_decay
                  <input type="number" step="0.001" value={trainForm.weight_decay} onChange={(e) => setTrainForm((f) => ({ ...f, weight_decay: e.target.value }))} />
                </label>
                <label>
                  warmup_ratio
                  <input type="number" step="0.01" value={trainForm.warmup_ratio} onChange={(e) => setTrainForm((f) => ({ ...f, warmup_ratio: e.target.value }))} />
                </label>
              </div>
            </details>

            <div className="tt-train-actions">
              <button className="tt-btn tt-btn-primary" type="submit" disabled={trainLoading}>
                {trainLoading ? "Lancement…" : "Lancer l'entraînement"}
              </button>
              {currentJob && ["pending", "running"].includes(currentJob.status) && (
                <button className="tt-btn tt-btn-danger" type="button" onClick={handleCancelTraining}>
                  Annuler le job en cours
                </button>
              )}
            </div>
          </form>
          {trainError && <p className="tt-hint tt-hint-error">{trainError}</p>}

          <TrainJobTracker
            job={currentJob}
            onCancel={handleCancelTraining}
            cancelLoading={false}
          />

          <div className="tt-jobs-controls">
            <label className="tt-jobs-filter">
              Statut
              <select
                className="tt-select"
                value={jobsStatusFilter}
                onChange={handleJobsStatusFilterChange}
              >
                <option value="">Tous</option>
                <option value="pending">pending</option>
                <option value="running">running</option>
                <option value="completed">completed</option>
                <option value="failed">failed</option>
                <option value="cancelled">cancelled</option>
              </select>
            </label>
            <div className="tt-jobs-pagination">
              <span className="tt-jobs-range">
                {jobsTotal > 0 && jobs.length > 0
                  ? `Jobs ${jobsOffset + 1}–${jobsOffset + jobs.length} sur ${jobsTotal}`
                  : `${jobsTotal} job(s)`}
              </span>
              <button
                type="button"
                className="tt-btn tt-btn-ghost"
                onClick={() => setJobsOffset(Math.max(0, jobsOffset - jobsLimit))}
                disabled={jobsOffset === 0}
              >
                ← Précédent
              </button>
              <button
                type="button"
                className="tt-btn tt-btn-ghost"
                onClick={() => setJobsOffset(jobsOffset + jobsLimit)}
                disabled={jobsOffset + jobsLimit >= jobsTotal}
              >
                Suivant →
              </button>
            </div>
          </div>

          <table className="tt-table tt-jobs-table">
            <caption className="sr-only">Historique des jobs d'entraînement</caption>
            <thead>
              <tr>
                <th scope="col">Job</th>
                <th scope="col">Statut</th>
                <th scope="col">Étape</th>
                <th scope="col">Démarré</th>
                <th scope="col">Terminé</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.job_id}>
                  <td className="tt-mono">{job.job_id.slice(0, 8)}</td>
                  <td>
                    <span className={`tt-tag tt-tag-status-${job.status}`}>{job.status}</span>
                  </td>
                  <td>{STEP_LABELS[job.step] || job.step}</td>
                  <td className="tt-mono">{formatEpoch(job.started_at)}</td>
                  <td className="tt-mono">{formatEpoch(job.finished_at)}</td>
                </tr>
              ))}
              {!jobs.length && (
                <tr>
                  <td colSpan={5} className="tt-hint">
                    Aucun job pour le moment.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
      </div>
    </>
  );
}
