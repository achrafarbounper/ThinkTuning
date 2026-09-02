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
import TrainMetricsStream from "../components/TrainMetricsStream";
import TrainingHistoryChart from "../components/TrainingHistoryChart";
import ModelSanityPanel from "../components/ModelSanityPanel";

const JOBS_POLL_MS = 15000;
const TRAIN_POLL_MS = 4000;
const SCHEDULES_POLL_MS = 30000;

const DAY_LABELS = ["Dimanche", "Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi"];

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

interface TrainJob {
  job_id: string;
  status: string;
  step: string;
  started_at: number;
  finished_at: number;
  regression?: boolean;
  regression_detail?: string;
}

interface ScheduledJob {
  schedule_id: string;
  next_run_at: number;
  [key: string]: unknown;
}

interface TrainForm {
  max_per_lang: number;
  augment_fraction: number;
  variants_per_example: number;
  device: string;
  epochs: string;
  batch_size: string;
  num_workers: string;
  max_length: string;
  learning_rate: string;
  weight_decay: string;
  warmup_ratio: string;
  base_model_version: string;
}

function numOrUndef(value: string | null | undefined): number | undefined {
  if (value === "" || value === null || value === undefined) return undefined;
  const n = Number(value);
  return Number.isNaN(n) ? undefined : n;
}

function formatEpoch(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  try {
    return new Date(seconds * 1000).toLocaleString();
  } catch {
    return "—";
  }
}

export default function TrainingPage() {
  const { client, refreshModels, pushLog, models, modelsError } = useApp();

  const [trainForm, setTrainForm] = useState<TrainForm>({
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
    // Continual training : version source (experiments/models) à partir
    // de laquelle reprendre l'entraînement. "" => modèle de base.
    base_model_version: "",
  });
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [currentJob, setCurrentJob] = useState<string | null>(null);
  const [currentJobData, setCurrentJobData] = useState<TrainJob | null>(null);
  const [trainLoading, setTrainLoading] = useState(false);
  const [trainError, setTrainError] = useState<string | null>(null);
  const [jobs, setJobs] = useState<TrainJob[]>([]);
  // Pagination / filtrage GET /train/jobs (?status=&limit=&offset=)
  const [jobsStatusFilter, setJobsStatusFilter] = useState("");
  const [jobsLimit] = useState(20);
  const [jobsOffset, setJobsOffset] = useState(0);
  const [jobsTotal, setJobsTotal] = useState(0);

  // -- Planification récurrente (SCRUM-34) --------------------------------------
  const [scheduleMode, setScheduleMode] = useState("interval"); // "interval" | "daily" | "weekly"
  const [scheduleTime, setScheduleTime] = useState("02:00");    // heure fixe
  const [scheduleDay, setScheduleDay] = useState("1");          // 0-6 (dimanche=0)
  const [scheduleInterval, setScheduleInterval] = useState("60");
  const [schedules, setSchedules] = useState<ScheduledJob[]>([]);
  const [scheduleLoading, setScheduleLoading] = useState(false);
  const [scheduleError, setScheduleError] = useState<string | null>(null);

  const trainPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // -- Continual training : s'assurer que la liste des versions est chargée ---
  useEffect(() => {
    if (!models.length && !modelsError) refreshModels();
  }, [models.length, modelsError, refreshModels]);

  // -- Historique des jobs -----------------------------------------------------
  const refreshJobs = useCallback(async () => {
    try {
      // Réponse : { total, items, limit, offset } trié par started_at DESC.
      const result = (await client.listTrainingJobs({
        status: jobsStatusFilter || undefined,
        limit: jobsLimit,
        offset: jobsOffset,
      })) as { items: TrainJob[]; total: number };
      setJobs(result.items);
      setJobsTotal(result.total);
    } catch (err: unknown) {
      pushLog("error", `Historique des jobs indisponible : ${(err as Error).message}`);
    }
  }, [client, pushLog, jobsStatusFilter, jobsLimit, jobsOffset]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        // Réponse : { total, items, limit, offset } trié par started_at DESC.
        const result = (await client.listTrainingJobs({
          status: jobsStatusFilter || undefined,
          limit: jobsLimit,
          offset: jobsOffset,
        })) as { items: TrainJob[]; total: number };
        if (!cancelled) {
          setJobs(result.items);
          setJobsTotal(result.total);
        }
      } catch (err: unknown) {
        if (!cancelled) {
          pushLog("error", `Historique des jobs indisponible : ${(err as Error).message}`);
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
    (jobId: string) => {
      stopTrainPolling();
      trainPollRef.current = setInterval(async () => {
        try {
          const job = (await client.getTrainingStatus(jobId)) as TrainJob;
          setCurrentJob(job.job_id);
          setCurrentJobData(job);
          if (["completed", "failed", "cancelled"].includes(job.status)) {
            stopTrainPolling();
            pushLog(
              job.status === "completed" ? "success" : "error",
              `Job ${jobId.slice(0, 8)} → ${job.status}${(job as unknown as { error?: string }).error ? ` (${(job as unknown as { error: string }).error.split("\n")[0]})` : ""}`
            );
            refreshModels();
            refreshJobs();
          }
        } catch (err: unknown) {
          pushLog("error", `Suivi du job interrompu : ${(err as Error).message}`);
          stopTrainPolling();
        }
      }, TRAIN_POLL_MS);
    },
    [client, stopTrainPolling, pushLog, refreshModels, refreshJobs]
  );

  // -- Actions -------------------------------------------------------------------
  const handleStartTraining = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setTrainError(null);
    setTrainLoading(true);
    const payload = {
      max_per_lang: numOrUndef(String(trainForm.max_per_lang)) ?? 500,
      augment_fraction: numOrUndef(String(trainForm.augment_fraction)) ?? 0.4,
      variants_per_example: numOrUndef(String(trainForm.variants_per_example)) ?? 2,
      epochs: numOrUndef(trainForm.epochs),
      batch_size: numOrUndef(trainForm.batch_size),
      num_workers: numOrUndef(trainForm.num_workers),
      max_length: numOrUndef(trainForm.max_length),
      learning_rate: numOrUndef(trainForm.learning_rate),
      weight_decay: numOrUndef(trainForm.weight_decay),
      warmup_ratio: numOrUndef(trainForm.warmup_ratio),
      device: trainForm.device || "auto",
      base_model_version: trainForm.base_model_version || null,
    };
    try {
      const job = (await client.startTraining(payload)) as TrainJob;
      setCurrentJob(job.job_id);
      pushLog(
        "info",
        `Entraînement lancé — job ${job.job_id.slice(0, 8)}` +
          (trainForm.base_model_version
            ? ` (continual depuis ${trainForm.base_model_version})`
            : "")
      );
      pollJob(job.job_id);
      refreshJobs();
    } catch (err: unknown) {
      setTrainError((err as Error).message);
    } finally {
      setTrainLoading(false);
    }
  };

  const handleCancelTraining = async () => {
    if (!currentJob) return;
    try {
      const job = (await client.cancelTraining(currentJob)) as TrainJob;
      setCurrentJob(job.job_id);
      pushLog("error", `Annulation demandée pour ${job.job_id.slice(0, 8)}`);
    } catch (err: unknown) {
      pushLog("error", `Échec de l'annulation : ${(err as Error).message}`);
    }
  };

  const handleJobsStatusFilterChange = (event: React.ChangeEvent<HTMLSelectElement>) => {
    setJobsStatusFilter(event.target.value);
    setJobsOffset(0); // repart à la première page quand le filtre change
  };

  // -- Planification récurrente (SCRUM-34) --------------------------------------
  const refreshSchedules = useCallback(async () => {
    try {
      const result = (await client.listSchedules()) as { items: ScheduledJob[] };
      setSchedules(result.items || []);
    } catch (err: unknown) {
      pushLog("error", `Planifications indisponibles : ${(err as Error).message}`);
    }
  }, [client, pushLog]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const result = (await client.listSchedules()) as { items: ScheduledJob[] };
        if (!cancelled) {
          setSchedules(result.items || []);
        }
      } catch (err: unknown) {
        if (!cancelled) {
          pushLog("error", `Planifications indisponibles : ${(err as Error).message}`);
        }
      }
    };
    load();
    const interval = setInterval(load, SCHEDULES_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [client, pushLog]);

  const handleCreateSchedule = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setScheduleError(null);
    setScheduleLoading(true);

    let cron: string | null = null;
    let intervalMinutes: number | null = null;
    if (scheduleMode === "interval") {
      intervalMinutes = numOrUndef(scheduleInterval) ?? null;
      if (!intervalMinutes || intervalMinutes <= 0) {
        setScheduleError("Intervalle invalide : renseignez un nombre de minutes > 0.");
        setScheduleLoading(false);
        return;
      }
    } else {
      const [h, m] = String(scheduleTime).split(":").map(Number);
      if (Number.isNaN(h) || Number.isNaN(m) || h < 0 || h > 23 || m < 0 || m > 59) {
        setScheduleError("Heure invalide.");
        setScheduleLoading(false);
        return;
      }
      cron =
        scheduleMode === "weekly"
          ? `${m} ${h} * * ${scheduleDay}` // hebdomadaire : min heure jour_du_mois mois jour_semaine
          : `${m} ${h} * * *`; // quotidien
    }

    // Paramètres d'entraînement identiques au formulaire POST /train.
    const train = {
      max_per_lang: numOrUndef(String(trainForm.max_per_lang)) ?? 500,
      augment_fraction: numOrUndef(String(trainForm.augment_fraction)) ?? 0.4,
      variants_per_example: numOrUndef(String(trainForm.variants_per_example)) ?? 2,
      epochs: numOrUndef(trainForm.epochs),
      batch_size: numOrUndef(trainForm.batch_size),
      num_workers: numOrUndef(trainForm.num_workers),
      max_length: numOrUndef(trainForm.max_length),
      learning_rate: numOrUndef(trainForm.learning_rate),
      weight_decay: numOrUndef(trainForm.weight_decay),
      warmup_ratio: numOrUndef(trainForm.warmup_ratio),
      device: trainForm.device || "auto",
      base_model_version: trainForm.base_model_version || null,
    };

    try {
      const schedule = (await client.scheduleTraining({ train, cron, interval_minutes: intervalMinutes })) as ScheduledJob;
      pushLog("success", `Entraînement programmé (${schedule.schedule_id.slice(0, 8)})`);
      refreshSchedules();
    } catch (err: unknown) {
      setScheduleError((err as Error).message);
    } finally {
      setScheduleLoading(false);
    }
  };

  const handleDeleteSchedule = async (scheduleId: string) => {
    try {
      await client.deleteSchedule(scheduleId);
      pushLog("error", `Planification ${scheduleId.slice(0, 8)} supprimée`);
      refreshSchedules();
    } catch (err: unknown) {
      pushLog("error", `Échec de la suppression : ${(err as Error).message}`);
    }
  };

  /** Libellé lisible d'une planification ("Toutes les 60 min" / "cron 0 2 * * *"). */
  function describeSchedule(schedule: ScheduledJob) {
    if ((schedule.trigger as string) === "cron") {
      const parts = ((schedule.cron as string) || "").split(" ");
      if (parts.length === 5) {
        const [minute, hour, , , dow] = parts;
        const time = `${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`;
        if (dow === "*") return `Quotidien à ${time}`;
        if (/^\d$/.test(dow)) return `${DAY_LABELS[Number(dow)]} à ${time}`;
      }
      return `cron ${schedule.cron as string}`;
    }
    return `Toutes les ${schedule.interval_minutes as number} min`;
  }

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
                  onChange={(e) => setTrainForm((f) => ({ ...f, max_per_lang: Number(e.target.value) }))}
                />
              </label>
              <label>
                augment_fraction
                <input
                  type="number"
                  step="0.05"
                  value={trainForm.augment_fraction}
                  onChange={(e) => setTrainForm((f) => ({ ...f, augment_fraction: Number(e.target.value) }))}
                />
              </label>
              <label>
                variants_per_example
                <input
                  type="number"
                  value={trainForm.variants_per_example}
                  onChange={(e) => setTrainForm((f) => ({ ...f, variants_per_example: Number(e.target.value) }))}
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
              {/* Continual training : reprise depuis une version précédente */}
              <label>
                Reprendre depuis
                <select
                  value={trainForm.base_model_version}
                  onChange={(e) => setTrainForm((f) => ({ ...f, base_model_version: e.target.value }))}
                >
                  <option value="">Modèle de base (from scratch)</option>
                  {models.map((model) => (
                    <option key={model.name} value={model.name}>
                      {model.name}
                      {model.active ? " (actif)" : ""}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {trainForm.base_model_version && (
              <p className="tt-hint">
                Continual training : l'entraînement reprendra les poids de la version{" "}
                <span className="tt-mono">{trainForm.base_model_version}</span> au lieu du
                modèle de base. Comparez le F1 de la nouvelle version avec l'ancienne
                (page Comparer) avant de la conserver.
              </p>
            )}

            <details
              className="tt-advanced"
              open={advancedOpen}
              onToggle={(e) => setAdvancedOpen((e.target as HTMLDetailsElement).open)}
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
              {currentJob && currentJobData && ["pending", "running"].includes(currentJobData.status) && (
                <button className="tt-btn tt-btn-danger" type="button" onClick={handleCancelTraining}>
                  Annuler le job en cours
                </button>
              )}
            </div>
          </form>
          {trainError && <p className="tt-hint tt-hint-error">{trainError}</p>}

          <TrainJobTracker
            job={currentJobData}
            onCancel={handleCancelTraining}
            cancelLoading={false}
          />

          {/* -- Métriques live (WebSocket /train/stream/{job_id}) ---------- */}
          {currentJob && <TrainMetricsStream jobId={currentJob} client={client} />}

          {/* -- Planification récurrente (SCRUM-34 : POST /train/schedule) --- */}
          <div className="tt-history-section">
            <h3 className="tt-subtitle">Planification récurrente</h3>
            <p className="tt-hint">
              Programme un entraînement à intervalle régulier ou à une heure fixe
              (les paramètres ci-dessus sont réutilisés à chaque exécution).
            </p>
            <form onSubmit={handleCreateSchedule} className="tt-form">
              <div className="tt-field-grid">
                <label>
                  Mode
                  <select
                    value={scheduleMode}
                    onChange={(e) => setScheduleMode(e.target.value)}
                  >
                    <option value="interval">Intervalle (minutes)</option>
                    <option value="daily">Chaque jour à…</option>
                    <option value="weekly">Chaque semaine à…</option>
                  </select>
                </label>
                {scheduleMode === "interval" ? (
                  <label>
                    Intervalle (minutes)
                    <input
                      type="number"
                      min="1"
                      value={scheduleInterval}
                      onChange={(e) => setScheduleInterval(e.target.value)}
                    />
                  </label>
                ) : (
                  <label>
                    Heure
                    <input
                      type="time"
                      value={scheduleTime}
                      onChange={(e) => setScheduleTime(e.target.value)}
                    />
                  </label>
                )}
                {scheduleMode === "weekly" && (
                  <label>
                    Jour
                    <select
                      value={scheduleDay}
                      onChange={(e) => setScheduleDay(e.target.value)}
                    >
                      {DAY_LABELS.map((label, index) => (
                        <option key={label} value={String(index)}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
              </div>
              <div className="tt-train-actions">
                <button className="tt-btn tt-btn-primary" type="submit" disabled={scheduleLoading}>
                  {scheduleLoading ? "Programmation…" : "Programmer l'entraînement"}
                </button>
              </div>
            </form>
            {scheduleError && <p className="tt-hint tt-hint-error">{scheduleError}</p>}

            <table className="tt-table">
              <caption className="sr-only">Planifications d'entraînement actives</caption>
              <thead>
                <tr>
                  <th scope="col">Planification</th>
                  <th scope="col">Prochaine exécution</th>
                  <th scope="col">Trigger</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {schedules.map((schedule) => (
                  <tr key={schedule.schedule_id}>
                    <td className="tt-mono">{schedule.schedule_id.slice(0, 8)}</td>
                    <td className="tt-mono">{formatEpoch(schedule.next_run_at)}</td>
                    <td>
                      <span className="tt-tag">
                        {describeSchedule(schedule)}
                      </span>
                    </td>
                    <td>
                      <button
                        type="button"
                        className="tt-btn tt-btn-danger"
                        onClick={() => handleDeleteSchedule(schedule.schedule_id)}
                      >
                        Supprimer
                      </button>
                    </td>
                  </tr>
                ))}
                {!schedules.length && (
                  <tr>
                    <td colSpan={4} className="tt-hint">
                      Aucune planification pour le moment.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="tt-history-section">
            <h3 className="tt-subtitle">Courbes loss / F1 par version de modèle</h3>
            <TrainingHistoryChart
              jobs={jobs as never}
              client={client}
              pushLog={pushLog as never}
            />
          </div>

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
                    {job.regression && (
                      <span
                        className="tt-tag tt-tag-status-failed"
                        title={job.regression_detail || "F1 inférieur à la version source"}
                        style={{ marginLeft: 6 }}
                      >
                        ⚠ régression
                      </span>
                    )}
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

        {/* Santé & nettoyage du répertoire experiments/models (SCRUM-74) */}
        <ModelSanityPanel
          client={client}
          models={models as never}
          onModelsChanged={refreshModels}
          pushLog={pushLog as never}
        />
      </div>
    </>
  );
}
