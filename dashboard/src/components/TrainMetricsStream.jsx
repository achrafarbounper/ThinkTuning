import { useEffect, useRef, useState } from "react";
import { useApp } from "../context/useApp";
import { TRAIN_STEPS } from "../api/jobSteps";
import StepLogConsole from "./StepLogConsole";

/**
 * TrainMetricsStream — suit un job d'entraînement via le WebSocket
 * GET /train/stream/{job_id} et affiche en temps réel :
 *   - le pourcentage global du pipeline,
 *   - l'avancement batch-par-batch (comme tqdm) des phases train/eval,
 *   - l'état de chaque étape du pipeline (done / active / pending / error),
 *   - les logs serveur correspondants (console live),
 *   - les métriques (loss / F1 macro / accuracy, epoch par epoch).
 *
 * Protocole serveur (JSON) :
 *   {"type": "epoch", "job_id", "epoch", "loss", "f1_macro", "accuracy"}
 *   {"type": "progress", ..., "steps": {<step>: {"status": ...}}}
 *   {"type": "log", "seq", "ts", "level", "step", "message"}
 *   {"type": "end", "status"}       → le serveur ferme la connexion
 *   {"type": "stalled", ...}        → plus de nouveauté depuis X minutes
 *   {"type": "error", "detail"}
 */
/** Libellés FR des étapes — alignés sur TrainJobTracker.jsx / jobSteps.js. */
const STEP_LABELS = {
  queued: "En file d'attente",
  loading_dataset: "Chargement du dataset",
  splitting_dataset: "Découpage train/val",
  augmenting_dataset: "Augmentation du dataset",
  building_dataloaders: "Préparation des dataloaders",
  computing_class_weights: "Calcul des poids de classe",
  loading_model: "Chargement du modèle",
  training: "Entraînement",
  saving_model: "Sauvegarde du modèle",
  labeling: "Labeling DistilBERT",
  filtering: "Filtrage",
  finetuning: "Fine-tuning",
  done: "Terminé",
  cancelled: "Annulé",
};

// Reconnexion automatique : le serveur rejoue les epochs existants à la
// reconnexion (dédup par epoch côté composant), donc c'est sans risque.
const RECONNECT_MAX_ATTEMPTS = 10;
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;

export default function TrainMetricsStream({ jobId }) {
  const { client } = useApp();
  const [epochs, setEpochs] = useState([]);
  const [state, setState] = useState("idle"); // idle | open | closed | stalled | error
  const [finalStatus, setFinalStatus] = useState(null);
  const [stallInfo, setStallInfo] = useState(null);
  const [currentStep, setCurrentStep] = useState(null);
  const [progress, setProgress] = useState(null);
  const [logs, setLogs] = useState([]);
  const wsRef = useRef(null);
  const reconnectAttemptRef = useRef(0);
  // Dédup des logs par seq (le serveur rejoue tout à la reconnexion).
  const seenSeqRef = useRef(new Set());

  useEffect(() => {
    // Reset à chaque changement de job / client : motif volontaire (le flux
    // est une ressource externe liée à jobId), règle trop stricte ici.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setEpochs([]);
    setFinalStatus(null);
    setStallInfo(null);
    setCurrentStep(null);
    setProgress(null);
    setLogs([]);
    seenSeqRef.current = new Set();
    reconnectAttemptRef.current = 0;

    // Ne se connecte que si une clé API est configurée (sinon le serveur
    // rejetterait la connexion : jeton manquant).
    if (!jobId || !client || !client.getTrainMetricsStreamUrl || !client.apiKey) {
      setState("idle");
      return undefined;
    }

    let cancelled = false;
    let backoffTimer = null;

    const connect = () => {
      let ws;
      try {
        ws = new WebSocket(client.getTrainMetricsStreamUrl(jobId));
      } catch {
        setState("error");
        return;
      }
      wsRef.current = ws;
      setState("open");

      // Fermeture "normale" : un message end/stalled/error a déjà été reçu,
      // pas besoin de reconnexion automatique.
      let normalClose = false;

      ws.onopen = () => {
        reconnectAttemptRef.current = 0;
      };
      ws.onmessage = (event) => {
        if (cancelled) return;
        let msg;
        try {
          msg = JSON.parse(event.data);
        } catch {
          return;
        }
        if (msg.type === "epoch") {
          setEpochs((prev) => {
            const next = prev.filter((e) => e.epoch !== msg.epoch);
            next.push(msg);
            next.sort((a, b) => a.epoch - b.epoch);
            return next;
          });
        } else if (msg.type === "step") {
          setCurrentStep(msg.step);
        } else if (msg.type === "progress") {
          setProgress(msg);
          if (msg.step) setCurrentStep(msg.step);
        } else if (msg.type === "log") {
          if (!seenSeqRef.current.has(msg.seq)) {
            seenSeqRef.current.add(msg.seq);
            setLogs((prev) => [...prev, msg]);
          }
        } else if (msg.type === "end") {
          normalClose = true;
          setFinalStatus(msg.status);
          setState("closed");
        } else if (msg.type === "stalled") {
          normalClose = true;
          setStallInfo(msg);
          setState("stalled");
        } else if (msg.type === "error") {
          normalClose = true;
          setStallInfo(null);
          setState("error");
        }
      };
      ws.onerror = () => {
        if (!cancelled) setState("error");
      };
      ws.onclose = () => {
        if (cancelled || normalClose) return;
        // Fermeture anormale (stall, réseau, restart serveur) : reconnexion
        // automatique avec backoff exponentiel.
        const attempt = reconnectAttemptRef.current;
        if (attempt >= RECONNECT_MAX_ATTEMPTS) {
          setState("error");
          return;
        }
        reconnectAttemptRef.current = attempt + 1;
        const delay = Math.min(RECONNECT_BASE_MS * 2 ** attempt, RECONNECT_MAX_MS);
        backoffTimer = setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (backoffTimer) clearTimeout(backoffTimer);
      // Fermeture propre du WebSocket au démontage / changement de job.
      const ws = wsRef.current;
      wsRef.current = null;
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        ws.close();
      }
    };
  }, [jobId, client]);

  if (!jobId || state === "idle") return null;

  const last = epochs.length ? epochs[epochs.length - 1] : null;
  const fmt = (v) => (v === null || v === undefined ? "—" : Number(v).toFixed(4));

  // --- Progression (global + par étape + batch) ---------------------------
  const stepsMap = progress?.steps || {};
  // Ordre canonique du serveur + étapes éventuelles inconnues (pipeline).
  const orderedSteps = [
    ...TRAIN_STEPS,
    ...Object.keys(stepsMap).filter((s) => !TRAIN_STEPS.includes(s)),
  ];
  const STATUS_BADGE = { done: "✓", active: "•", error: "✗", pending: "·" };
  const globalPct = progress?.global_pct;
  const batchLine =
    progress?.batch != null && progress?.batches_total != null
      ? `Batch ${progress.batch}/${progress.batches_total} · ${progress.batch_pct ?? 0} %`
      : null;
  const rateLine =
    progress?.rate_it_s != null ? `${Number(progress.rate_it_s).toFixed(2)} s/it` : null;
  const etaLine =
    progress?.eta != null ? `ETA ${Number(progress.eta).toFixed(0)} s` : null;

  return (
    <div className="tt-metrics-stream">
      <h3 className="tt-subtitle">Métriques en direct</h3>
      <p className="tt-hint">
        Flux WebSocket <code>/train/stream/{jobId.slice(0, 8)}</code>
        {state === "open" && " · connecté ⟳"}
        {currentStep && ` · Étape : ${STEP_LABELS[currentStep] || currentStep}`}
        {state === "stalled" && stallInfo &&
          ` · aucune nouvelle epoch depuis ${stallInfo.minutes} min`}
        {state === "closed" && finalStatus && ` · flux terminé (${finalStatus})`}
        {state === "error" && " · flux indisponible"}
      </p>

      {/* -- Pourcentage global (toutes étapes confondues) ------------------ */}
      {globalPct != null && (
        <div className="tt-global-progress">
          <div className="tt-global-progress-header">
            <span>Progression globale</span>
            <strong>{Number(globalPct).toFixed(1)} %</strong>
          </div>
          <div className="tt-progress-track">
            <div
              className="tt-progress-fill"
              style={{ width: `${Math.min(100, Math.max(0, globalPct))}%` }}
            />
          </div>
        </div>
      )}

      {/* -- État de chaque étape du pipeline -------------------------------- */}
      {progress?.steps && (
        <ul className="tt-steps-status">
          {orderedSteps.map((s) => {
            const status = stepsMap[s]?.status || "pending";
            return (
              <li
                key={s}
                className={`tt-step-status tt-step-${status}${
                  s === currentStep ? " tt-step-current" : ""
                }`}
              >
                <span className="tt-step-badge">{STATUS_BADGE[status] || "·"}</span>
                <span className="tt-step-name">{STEP_LABELS[s] || s}</span>
              </li>
            );
          })}
        </ul>
      )}

      {/* -- Avancement batch-par-batch (phases train / eval) ---------------- */}
      {(batchLine || rateLine || etaLine) && (
        <p className="tt-batch-live">
          {progress?.phase && `${progress.phase === "eval" ? "Éval" : "Train"} — `}
          {batchLine}
          {rateLine && ` · ${rateLine}`}
          {etaLine && ` · ${etaLine}`}
        </p>
      )}

      {/* -- Logs serveur live ---------------------------------------------- */}
      {logs.length > 0 && (
        <StepLogConsole
          logs={logs}
          stepLabels={STEP_LABELS}
          currentStep={currentStep}
        />
      )}

      {last && (
        <p className="tt-metrics-live">
          Epoch {last.epoch} · <strong>Loss {fmt(last.loss)}</strong> ·{" "}
          <strong>F1 {fmt(last.f1_macro)}</strong> · Acc {fmt(last.accuracy)}
        </p>
      )}

      {epochs.length > 0 && (
        <table className="tt-metrics-table">
          <thead>
            <tr>
              <th>Epoch</th>
              <th>Loss</th>
              <th>F1 macro</th>
              <th>Accuracy</th>
            </tr>
          </thead>
          <tbody>
            {epochs.map((e) => (
              <tr key={e.epoch}>
                <td>{e.epoch}</td>
                <td>{fmt(e.loss)}</td>
                <td>{fmt(e.f1_macro)}</td>
                <td>{fmt(e.accuracy)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}