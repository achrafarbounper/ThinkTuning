/**
 * Page « Annotation » — cycle d'amélioration continue du modèle (SCRUM-55).
 *
 * 1. Chargement des exemples incertains (POST /active_learning, confiance ~ 1/3),
 * 2. Annotation manuelle via les boutons negative / neutral / positive
 *    (POST /annotate, dédupliquée par texte),
 * 3. Lancement du cycle complet : fusion -> ré-entraînement -> activation
 *    (POST /active_learning/cycle, suivi du job par polling).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "../context/useApp";

const POLL_MS = 4000;

interface AnnotLabel {
  value: string;
  label: string;
  className: string;
}

const LABELS: AnnotLabel[] = [
  { value: "negative", label: "Négatif", className: "annot-btn annot-btn--negative" },
  { value: "neutral", label: "Neutre", className: "annot-btn annot-btn--neutral" },
  { value: "positive", label: "Positif", className: "annot-btn annot-btn--positive" },
];

const STEP_LABELS: Record<string, string> = {
  merging_annotations: "Fusion des annotations",
  training: "Ré-entraînement",
  activation: "Activation du modèle",
  done: "Terminé",
  failed: "Échec",
};

interface AnnotationItem {
  text: string;
  label: string;
}

interface AnnotationsList {
  total: number;
  items: AnnotationItem[];
}

interface UncertainExample {
  text: string;
  predicted_label: string;
  confidence: number;
  uncertainty: number;
}

interface CycleJob {
  job_id: string;
  status: string;
  error?: string | null;
  regression?: boolean;
  regression_detail?: string;
  progress?: {
    cycle?: Record<string, unknown>;
  };
}

function pct(value: unknown): string {
  const n = Number(value);
  return Number.isNaN(n) ? "—" : `${Math.round(n * 100)}%`;
}

export default function AnnotationPage() {
  const { client, pushLog } = useApp();

  const [examples, setExamples] = useState<UncertainExample[]>([]);
  const [loadingExamples, setLoadingExamples] = useState(false);
  const [examplesError, setExamplesError] = useState<string | null>(null);
  const [annotated, setAnnotated] = useState<Record<string, string>>({});
  const [annotations, setAnnotations] = useState<AnnotationsList>({ total: 0, items: [] });
  const [activeVersion, setActiveVersion] = useState<string | null>(null);

  const [cycleJob, setCycleJob] = useState<CycleJob | null>(null);
  const [cycleLoading, setCycleLoading] = useState(false);
  const [cycleError, setCycleError] = useState<string | null>(null);

  const pollRef = useRef<number | null>(null);

  const refreshAnnotations = useCallback(async () => {
    try {
      setAnnotations(
        (await client.listAnnotations({ limit: 20 })) as AnnotationsList
      );
    } catch {
      /* silencieux : la liste est indicative */
    }
  }, [client]);

  useEffect(() => {
    let cancelled = false;
    const init = async () => {
      try {
        const data = await client.listAnnotations({ limit: 20 });
        if (!cancelled) setAnnotations(data as AnnotationsList);
      } catch {
        /* silencieux : la liste est indicative */
      }
      try {
        const data = (await client.getActiveModel()) as { version?: string } | null;
        if (!cancelled) setActiveVersion(data?.version || null);
      } catch {
        if (!cancelled) setActiveVersion(null);
      }
    };
    init();
    return () => {
      cancelled = true;
    };
  }, [client]);


  // Suivi du job de cycle (poll 4 s, arrêté sur statut terminal).
  useEffect(() => {
    if (!cycleJob?.job_id) return undefined;
    const jobId = cycleJob.job_id;
    const tick = async () => {
      try {
        const job = (await client.getActiveLearningCycleStatus(jobId)) as CycleJob;
        setCycleJob(job);
        if (job.status === "completed" || job.status === "failed") {
          if (pollRef.current) clearInterval(pollRef.current);
          pollRef.current = null;
          setCycleLoading(false);
          pushLog?.(
            job.status === "completed" ? "info" : "error",
            job.status === "completed"
              ? `Cycle terminé (job ${jobId.slice(0, 8)})`
              : `Cycle échoué : ${job.error || "?"}`
          );
          refreshAnnotations();
        }
      } catch {
        /* prochain tick */
      }
    };
    tick();
    pollRef.current = setInterval(tick, POLL_MS);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = null;
    };
  }, [cycleJob?.job_id, client, pushLog, refreshAnnotations]);

  const loadExamples = async () => {
    setLoadingExamples(true);
    setExamplesError(null);
    try {
      const data = (await client.getActiveLearning({ topN: 30 })) as {
        items?: UncertainExample[];
      };
      setExamples(data.items || []);
      setAnnotated({});
    } catch (err) {
      setExamplesError(err instanceof Error ? err.message : "Erreur de chargement");
    } finally {
      setLoadingExamples(false);
    }
  };

  const submitAnnotation = async (text: string, label: string) => {
    try {
      await client.annotate({ text, label });
      setAnnotated((prev) => ({ ...prev, [text]: label }));
      refreshAnnotations();
      pushLog?.("info", `Annotation enregistrée : "${text.slice(0, 40)}…" → ${label}`);
    } catch (err) {
      pushLog?.(
        "error",
        `Annotation refusée : ${err instanceof Error ? err.message : "?"}`
      );
    }
  };

  const startCycle = async () => {
    setCycleError(null);
    setCycleLoading(true);
    try {
      const job = (await client.startActiveLearningCycle({})) as CycleJob;
      setCycleJob(job);
    } catch (err) {
      setCycleError(err instanceof Error ? err.message : "Impossible de lancer le cycle");
      setCycleLoading(false);
    }
  };


  // Étape courante du cycle : première étape du pipeline dont le résultat est absent.
  const CYCLE_STEPS = ["merging_annotations", "training", "activation"];
  const cycleStep =
    cycleJob?.status === "running"
      ? CYCLE_STEPS.find((s) => !cycleJob?.progress?.cycle?.[s])
      : null;
  const pendingCount = examples.length - Object.keys(annotated).length;

  return (
    <section className="page">
      <header className="page__header">
        <h1>Annotation & amélioration continue</h1>
        <p>
          Annotez les exemples incertains (confiance proche de 1/3), puis lancez
          le cycle complet : fusion → ré-entraînement → activation.
        </p>
        {activeVersion && (
          <p className="annot-active">
            Version active : <strong>{activeVersion}</strong>
          </p>
        )}
      </header>

      <div className="annot-toolbar">
        <button type="button" className="tt-btn tt-btn--primary" onClick={loadExamples} disabled={loadingExamples}>
          {loadingExamples ? "Chargement…" : "Charger des exemples incertains"}
        </button>
        <span className="annot-count">
          {examples.length} exemple(s) — {pendingCount} en attente
        </span>
        <button
          type="button"
          className="tt-btn"
          onClick={startCycle}
          disabled={cycleLoading || annotations.total === 0}
          title={annotations.total === 0 ? "Annotez au moins un exemple avant de lancer le cycle" : "Fusion + ré-entraînement + activation"}
        >
          {cycleLoading ? "Cycle en cours…" : "Lancer le cycle complet"}
        </button>
      </div>

      {examplesError && <p className="tt-error">{examplesError}</p>}
      {cycleError && <p className="tt-error">{cycleError}</p>}

      {cycleJob && (
        <div className="annot-cycle">
          <h2>Cycle #{cycleJob.job_id.slice(0, 8)}</h2>
          <p>
            Statut : <strong>{cycleJob.status}</strong>
            {cycleStep && <> — étape : {STEP_LABELS[cycleStep] || cycleStep}</>}
          </p>
          {cycleJob.regression && (
            <p className="tt-warning">Régression détectée : {cycleJob.regression_detail}</p>
          )}
          {cycleJob.progress?.cycle?.activation ? (
            <pre className="annot-activation">
              {JSON.stringify(cycleJob.progress.cycle.activation, null, 2)}
            </pre>
          ) : null}
        </div>
      )}

      <div className="annot-list">
        {examples.map((ex) => (
          <article key={ex.text} className={`annot-card${annotated[ex.text] ? " annot-card--done" : ""}`}>
            <p className="annot-card__text">{ex.text}</p>
            <p className="annot-card__meta">
              Prédiction : <strong>{ex.predicted_label}</strong> · confiance {pct(ex.confidence)} · incertitude {pct(ex.uncertainty)}
            </p>
            <div className="annot-card__actions">
              {LABELS.map((l) => (
                <button
                  key={l.value}
                  type="button"
                  className={`${l.className}${annotated[ex.text] === l.value ? " annot-btn--selected" : ""}`}
                  onClick={() => submitAnnotation(ex.text, l.value)}
                  disabled={Boolean(annotated[ex.text])}
                >
                  {l.label}
                </button>
              ))}
            </div>
          </article>
        ))}
      </div>

      {annotations.total > 0 && (
        <div className="annot-stored">
          <h2>Annotations enregistrées ({annotations.total})</h2>
          <ul>
            {annotations.items.map((a) => (
              <li key={a.text}>
                « {a.text} » → <strong>{a.label}</strong>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

