/**
 * Page « Pipeline » — enchaînement automatique en un clic :
 * labeling DistilBERT -> filtrage par confidence -> fine-tuning LLM (LoRA).
 *
 * - Formulaire POST /pipeline (chemin serveur du fichier d'entrée + options),
 * - suivi temps réel du job (poll 4 s) via PipelineJobTracker,
 * - historique des jobs (GET /pipeline/jobs) avec filtre statut + pagination.
 */

import { useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { useApp } from "../context/useApp";
import ModelVersionSelector from "../components/ModelVersionSelector";
import PipelineJobTracker from "../components/PipelineJobTracker";

const PIPELINE_POLL_MS = 4000;

const STEP_LABELS: Record<string, string> = {
  queued: "En file d'attente",
  labeling: "Labeling DistilBERT",
  filtering: "Filtrage par confidence",
  finetuning: "Fine-tuning LLM (LoRA)",
  done: "Terminé",
  cancelled: "Annulé",
};

interface PipelineForm {
  input_path: string;
  labeled_out: string;
  output_dir: string;
  text_column: string;
  min_confidence: number;
  label_batch_size: string;
  model_path: string;
  base_model: string;
  validation_file: string;
  epochs: string;
  finetune_batch_size: string;
  learning_rate: string;
  lora_r: string;
  lora_alpha: string;
  use_qlora: boolean;
}

interface PipelineJob {
  job_id: string;
  status: string;
  step?: string;
  error?: string | null;
  started_at?: number;
  finished_at?: number;
}

interface PipelineJobsResult {
  items: PipelineJob[];
  total: number;
}

function numOrUndef(value: unknown): number | undefined {
  if (value === "" || value === null || value === undefined) return undefined;
  const n = Number(value);
  return Number.isNaN(n) ? undefined : n;
}

function formatEpoch(seconds?: number | string): string {
  if (!seconds) return "—";
  try {
    return new Date(Number(seconds) * 1000).toLocaleString();
  } catch {
    return "—";
  }
}

export default function PipelinePage() {
  const { client, models, modelsError, refreshModels, pushLog } = useApp();

  const [form, setForm] = useState<PipelineForm>({
    input_path: "",
    labeled_out: "",
    output_dir: "",
    text_column: "text",
    min_confidence: 0.7,
    label_batch_size: "32",
    model_path: "",
    base_model: "",
    validation_file: "",
    epochs: "",
    finetune_batch_size: "",
    learning_rate: "",
    lora_r: "",
    lora_alpha: "",
    use_qlora: true,
  });
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [currentJob, setCurrentJob] = useState<PipelineJob | null>(null);
  const [pipelineLoading, setPipelineLoading] = useState(false);
  const [pipelineError, setPipelineError] = useState<string | null>(null);
  const [cancelLoading, setCancelLoading] = useState(false);
  const [jobs, setJobs] = useState<PipelineJob[]>([]);
  const [jobsStatusFilter, setJobsStatusFilter] = useState("");
  const [jobsLimit] = useState(20);
  const [jobsOffset, setJobsOffset] = useState(0);
  const [jobsTotal, setJobsTotal] = useState(0);

  const pipelinePollRef = useRef<number | null>(null);

  const stopPipelinePolling = useCallback(() => {
    if (pipelinePollRef.current) {
      clearInterval(pipelinePollRef.current);
      pipelinePollRef.current = null;
    }
  }, []);

  const refreshJobs = useCallback(async () => {
    try {
      const result = (await client.listPipelineJobs({
        status: jobsStatusFilter || undefined,
        limit: jobsLimit,
        offset: jobsOffset,
      })) as PipelineJobsResult;
      setJobs(result.items);
      setJobsTotal(result.total);
    } catch (err) {
      pushLog("error", `Historique des jobs pipeline indisponible : ${err instanceof Error ? err.message : "?"}`);
    }
  }, [client, pushLog, jobsStatusFilter, jobsLimit, jobsOffset]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const result = (await client.listPipelineJobs({
          status: jobsStatusFilter || undefined,
          limit: jobsLimit,
          offset: jobsOffset,
        })) as PipelineJobsResult;
        if (!cancelled) {
          setJobs(result.items);
          setJobsTotal(result.total);
        }
      } catch (err) {
        if (!cancelled) {
          pushLog("error", `Historique des jobs pipeline indisponible : ${err instanceof Error ? err.message : "?"}`);
        }
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [client, pushLog, jobsStatusFilter, jobsLimit, jobsOffset]);

  useEffect(() => {
    if (!models.length && !modelsError) refreshModels();
  }, [models.length, modelsError, refreshModels]);

  // Suivi temps réel du job en cours (poll 4 s) tant qu'il est actif.
  const startPipelinePolling = useCallback(
    (jobId: string) => {
      stopPipelinePolling();
      pipelinePollRef.current = setInterval(async () => {
        try {
          const job = (await client.getPipelineStatus(jobId)) as PipelineJob;
          setCurrentJob(job);
          if (!["pending", "running"].includes(job.status)) {
            stopPipelinePolling();
            pushLog(
              "info",
              `Pipeline ${jobId.slice(0, 8)} → ${job.status}${job.error ? ` (${job.error.split("\n")[0]})` : ""}`
            );
            refreshJobs();
            refreshModels();
          }
        } catch (err) {
          stopPipelinePolling();
          pushLog("error", `Suivi du pipeline interrompu : ${err instanceof Error ? err.message : "?"}`);
        }
      }, PIPELINE_POLL_MS);
    },
    [client, stopPipelinePolling, pushLog, refreshJobs, refreshModels]
  );

  useEffect(() => stopPipelinePolling, [stopPipelinePolling]);

  const handleStartPipeline = async (event: FormEvent) => {
    event.preventDefault();
    if (!form.input_path.trim()) {
      setPipelineError("Le chemin du fichier d'entrée est requis.");
      return;
    }
    setPipelineLoading(true);
    setPipelineError(null);
    try {
      const payload: Record<string, unknown> = {
        input_path: form.input_path.trim(),
        text_column: form.text_column.trim() || "text",
        min_confidence: numOrUndef(form.min_confidence) ?? 0.7,
        label_batch_size: numOrUndef(form.label_batch_size) ?? 32,
        use_qlora: form.use_qlora,
      };
      if (form.labeled_out.trim()) payload.labeled_output = form.labeled_out.trim();
      if (form.output_dir.trim()) payload.output_dir = form.output_dir.trim();
      if (form.model_path) payload.model_path = form.model_path;
      if (form.base_model.trim()) payload.base_model = form.base_model.trim();
      if (form.validation_file.trim()) payload.validation_file = form.validation_file.trim();
      const optional = {
        epochs: form.epochs,
        finetune_batch_size: form.finetune_batch_size,
        learning_rate: form.learning_rate,
        lora_r: form.lora_r,
        lora_alpha: form.lora_alpha,
      };
      for (const [key, value] of Object.entries(optional)) {
        const n = numOrUndef(value);
        if (n !== undefined) payload[key] = n;
      }

      const job = (await client.startPipeline(payload)) as PipelineJob;
      setCurrentJob(job);
      pushLog("info", `Pipeline lancé (${job.job_id.slice(0, 8)})`);
      startPipelinePolling(job.job_id);
      refreshJobs();
    } catch (err) {
      setPipelineError(err instanceof Error ? err.message : String(err));
      pushLog("error", `Échec du lancement du pipeline : ${err instanceof Error ? err.message : "?"}`);
    } finally {
      setPipelineLoading(false);
    }
  };

  const handleCancelPipeline = async () => {
    if (!currentJob) return;
    setCancelLoading(true);
    try {
      const job = (await client.cancelPipeline(currentJob.job_id)) as PipelineJob;
      setCurrentJob(job);
      stopPipelinePolling();
      pushLog("info", `Pipeline ${job.job_id.slice(0, 8)} annulé`);
      refreshJobs();
    } catch (err) {
      pushLog("error", `Échec de l'annulation : ${err instanceof Error ? err.message : "?"}`);
    } finally {
      setCancelLoading(false);
    }
  };

  const handleJobsStatusFilterChange = (event: ChangeEvent<HTMLSelectElement>) => {
    setJobsStatusFilter(event.target.value);
    setJobsOffset(0);
  };

  const setField =
    (key: keyof PipelineForm) => (e: ChangeEvent<HTMLInputElement>) =>
      setForm((f) => ({ ...f, [key]: e.target.value } as PipelineForm));

  return (
    <>
      <header className="page-head">
        <h1>Pipeline end-to-end</h1>
        <p>
          Labeling DistilBERT → filtrage par confidence → fine-tuning LLM (LoRA),
          enchaînés automatiquement en un seul job.
        </p>
      </header>

      <div className="page-body">
        <section className="tt-panel">
          <div className="tt-panel-head">
            <h2>Lancer le pipeline</h2>
            <button className="tt-btn tt-btn-ghost" onClick={refreshJobs} type="button">
              Rafraîchir l'historique
            </button>
          </div>

          <form onSubmit={handleStartPipeline} className="tt-form tt-train-form">
            <div className="tt-field-grid">
              <label className="tt-field-wide">
                Fichier d'entrée (chemin serveur : CSV, JSON, JSONL ou TXT)
                <input
                  type="text"
                  placeholder="data/unlabeled.csv"
                  value={form.input_path}
                  onChange={setField("input_path")}
                />
              </label>
              <label>
                Colonne texte
                <input type="text" value={form.text_column} onChange={setField("text_column")} />
              </label>
              <label>
                Seuil de confidence (min_confidence)
                <input
                  type="number"
                  step="0.05"
                  min="0"
                  max="1"
                  value={form.min_confidence}
                  onChange={setField("min_confidence")}
                />
              </label>
              <label>
                JSONL labelé (optionnel)
                <input
                  type="text"
                  placeholder="experiments/pipeline/&lt;job&gt;/labeled.jsonl"
                  value={form.labeled_out}
                  onChange={setField("labeled_out")}
                />
              </label>
              <label>
                Dossier du modèle LoRA (optionnel)
                <input
                  type="text"
                  placeholder="experiments/pipeline/&lt;job&gt;/lora_model"
                  value={form.output_dir}
                  onChange={setField("output_dir")}
                />
              </label>
            </div>

            <ModelVersionSelector
              models={models}
              activeModel={form.model_path}
              onModelChange={(value) => setForm((f) => ({ ...f, model_path: value }))}
              loading={!models.length && !modelsError}
              label="Modèle de labeling (DistilBERT)"
            />

            <details
              className="tt-advanced"
              open={advancedOpen}
              onToggle={(e) => setAdvancedOpen(e.currentTarget.open)}
            >
              <summary className="tt-advanced-toggle">Options fine-tuning avancées</summary>
              <div className="tt-field-grid">
                <label>
                  base_model
                  <input
                    type="text"
                    placeholder="TinyLlama/TinyLlama-1.1B-Chat-v1.0"
                    value={form.base_model}
                    onChange={setField("base_model")}
                  />
                </label>
                <label>
                  validation_file (optionnel)
                  <input type="text" value={form.validation_file} onChange={setField("validation_file")} />
                </label>
                <label>
                  epochs
                  <input type="number" value={form.epochs} onChange={setField("epochs")} />
                </label>
                <label>
                  finetune_batch_size
                  <input type="number" value={form.finetune_batch_size} onChange={setField("finetune_batch_size")} />
                </label>
                <label>
                  learning_rate
                  <input type="number" step="0.00001" value={form.learning_rate} onChange={setField("learning_rate")} />
                </label>
                <label>
                  lora_r
                  <input type="number" value={form.lora_r} onChange={setField("lora_r")} />
                </label>
                <label>
                  lora_alpha
                  <input type="number" value={form.lora_alpha} onChange={setField("lora_alpha")} />
                </label>
                <label>
                  label_batch_size
                  <input type="number" value={form.label_batch_size} onChange={setField("label_batch_size")} />
                </label>
                <label className="tt-checkbox">
                  <input
                    type="checkbox"
                    checked={form.use_qlora}
                    onChange={(e) => setForm((f) => ({ ...f, use_qlora: e.target.checked }))}
                  />
                  QLoRA (quantification 4-bit, GPU)
                </label>
              </div>
            </details>

            <div className="tt-train-actions">
              <button className="tt-btn tt-btn-primary" type="submit" disabled={pipelineLoading}>
                {pipelineLoading ? "Lancement…" : "Lancer le pipeline"}
              </button>
              {currentJob && ["pending", "running"].includes(currentJob.status) && (
                <button className="tt-btn tt-btn-danger" type="button" onClick={handleCancelPipeline} disabled={cancelLoading}>
                  Annuler le job en cours
                </button>
              )}
            </div>
          </form>
          {pipelineError && <p className="tt-hint tt-hint-error">{pipelineError}</p>}

          <PipelineJobTracker
            job={currentJob as never}
            onCancel={handleCancelPipeline}
            cancelLoading={cancelLoading}
          />

          <div className="tt-jobs-controls">
            <label className="tt-jobs-filter">
              Statut
              <select className="tt-select" value={jobsStatusFilter} onChange={handleJobsStatusFilterChange}>
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
            <caption className="sr-only">Historique des jobs de pipeline</caption>
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
                  <td>{STEP_LABELS[job.step ?? ""] || job.step}</td>
                  <td className="tt-mono">{formatEpoch(job.started_at)}</td>
                  <td className="tt-mono">{formatEpoch(job.finished_at)}</td>
                </tr>
              ))}
              {!jobs.length && (
                <tr>
                  <td colSpan={5} className="tt-hint">
                    Aucun job pipeline pour le moment.
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
