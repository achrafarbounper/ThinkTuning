import React, { useCallback, useEffect, useRef, useState } from "react";
import { SentimentApiClient, TRAIN_STEPS } from "./sentimentApiClient";
import TrainJobTracker from "./components/TrainJobTracker";

/* ------------------------------------------------------------------------
 * ThinkTuning — console d'exploitation
 * Panneaux : santé API, versions de modèles, prédiction (unitaire + batch
 * CSV), pilotage d'entraînement avec suivi d'étapes réel (poll 4s) et
 * historique des jobs.
 * ------------------------------------------------------------------------ */

const CONFIG_STORAGE_KEY = "thinktuning.apiConfig";
const PREDICTIONS_HISTORY_KEY = "thinktuning.predictionsHistory";
const MAX_HISTORY_SIZE_KEY = "thinktuning.maxHistorySize";
const HEALTH_POLL_MS = 8000;
const JOBS_POLL_MS = 15000;
const TRAIN_POLL_MS = 4000;
const DEFAULT_MAX_HISTORY = 20;

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

const SENTIMENT_LABELS = { negative: "négatif", neutral: "neutre", positive: "positif" };

function loadStoredConfig() {
  try {
    const raw = window.localStorage.getItem(CONFIG_STORAGE_KEY);
    if (!raw) return { baseUrl: "http://localhost:8000", apiKey: "" };
    const parsed = JSON.parse(raw);
    return {
      baseUrl: parsed.baseUrl || "http://localhost:8000",
      apiKey: parsed.apiKey || "",
    };
  } catch (_) {
    return { baseUrl: "http://localhost:8000", apiKey: "" };
  }
}

function loadStoredPredictionsHistory() {
  try {
    const raw = window.localStorage.getItem(PREDICTIONS_HISTORY_KEY);
    if (!raw) return [];
    return JSON.parse(raw);
  } catch (_) {
    return [];
  }
}

function loadStoredMaxHistorySize() {
  try {
    const raw = window.localStorage.getItem(MAX_HISTORY_SIZE_KEY);
    if (!raw) return DEFAULT_MAX_HISTORY;
    const size = parseInt(raw, 10);
    return Number.isNaN(size) ? DEFAULT_MAX_HISTORY : Math.max(1, size);
  } catch (_) {
    return DEFAULT_MAX_HISTORY;
  }
}

function saveHistoryToStorage(history) {
  try {
    window.localStorage.setItem(PREDICTIONS_HISTORY_KEY, JSON.stringify(history));
  } catch (_) {
    /* stockage indisponible */
  }
}

function saveSizeToStorage(size) {
  try {
    window.localStorage.setItem(MAX_HISTORY_SIZE_KEY, String(size));
  } catch (_) {
    /* stockage indisponible */
  }
}

function numOrUndef(value) {
  if (value === "" || value === null || value === undefined) return undefined;
  const n = Number(value);
  return Number.isNaN(n) ? undefined : n;
}

function formatEpoch(seconds) {
  if (!seconds) return "—";
  try {
    return new Date(seconds * 1000).toLocaleString();
  } catch (_) {
    return "—";
  }
}

export default function Dashboard() {
  const [config, setConfig] = useState(loadStoredConfig);
  const [configDraft, setConfigDraft] = useState(config);
  const clientRef = useRef(new SentimentApiClient(config));

  const [health, setHealth] = useState(null);
  const [healthError, setHealthError] = useState(null);

  const [models, setModels] = useState([]);
  const [modelsError, setModelsError] = useState(null);
  const [activeModel, setActiveModel] = useState("");

  const [predictText, setPredictText] = useState(
    "Ce film était vraiment excellent, j'ai adoré chaque instant.\nThis product is terrible, I want a refund.\nC'était correct, sans plus."
  );
  const [predictResults, setPredictResults] = useState(null);
  const [predictLoading, setPredictLoading] = useState(false);
  const [predictError, setPredictError] = useState(null);

  const [batchFile, setBatchFile] = useState(null);
  const [batchColumn, setBatchColumn] = useState("text");
  const [batchFormat, setBatchFormat] = useState("json");
  const [batchResults, setBatchResults] = useState(null);
  const [batchLoading, setBatchLoading] = useState(false);
  const [batchError, setBatchError] = useState(null);

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

  const [logs, setLogs] = useState([]);
  const logIdRef = useRef(0);
  const trainPollRef = useRef(null);

  const [predictionsHistory, setPredictionsHistory] = useState(loadStoredPredictionsHistory);
  const [maxHistorySize, setMaxHistorySizeState] = useState(loadStoredMaxHistorySize);

  const pushLog = useCallback((type, text) => {
    logIdRef.current += 1;
    setLogs((prev) =>
      [{ id: logIdRef.current, type, text, ts: Date.now() }, ...prev].slice(0, 25)
    );
  }, []);

  // -- Config: persist + keep the client instance in sync -----------------
  useEffect(() => {
    clientRef.current.setConfig(config);
    try {
      window.localStorage.setItem(CONFIG_STORAGE_KEY, JSON.stringify(config));
    } catch (_) {
      /* stockage indisponible, on continue sans persistance */
    }
  }, [config]);

  const refreshModels = useCallback(async () => {
    if (!config.apiKey) return;
    try {
      const list = await clientRef.current.listModels();
      setModels(list);
      setModelsError(null);
    } catch (err) {
      setModelsError(err.message);
    }
  }, [config.apiKey]);

  const refreshJobs = useCallback(async () => {
    if (!config.apiKey) return;
    try {
      const list = await clientRef.current.listTrainingJobs();
      setJobs(list.sort((a, b) => (b.started_at || 0) - (a.started_at || 0)));
    } catch (err) {
      pushLog("error", `Historique des jobs indisponible : ${err.message}`);
    }
  }, [config.apiKey, pushLog]);

  // -- Health : ne nécessite pas de clé API (voir GET /health dans api.py) --
  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const result = await clientRef.current.getHealth();
        if (!cancelled) {
          setHealth(result);
          setHealthError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setHealth(null);
          setHealthError(err.message);
        }
      }
    };
    poll();
    const interval = setInterval(poll, HEALTH_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [config.baseUrl]);

  // -- Modèles + jobs : nécessitent la clé API -----------------------------
  useEffect(() => {
    refreshModels();
    refreshJobs();
    const interval = setInterval(() => {
      refreshModels();
      refreshJobs();
    }, JOBS_POLL_MS);
    return () => clearInterval(interval);
  }, [refreshModels, refreshJobs]);

  // -- Nettoyage du polling d'entraînement au démontage --------------------
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
          const job = await clientRef.current.getTrainingStatus(jobId);
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
    [stopTrainPolling, pushLog, refreshModels, refreshJobs]
  );

  // -- Actions : prédiction unitaire ---------------------------------------
  const handlePredict = async (e) => {
    e.preventDefault();
    const texts = predictText
      .split("\n")
      .map((t) => t.trim())
      .filter(Boolean);
    if (!texts.length) return;

    setPredictLoading(true);
    setPredictError(null);
    try {
      const { results } = await clientRef.current.predict(texts, activeModel || undefined);
      setPredictResults(results);
      addToHistory(results);
    } catch (err) {
      setPredictError(err.message);
    } finally {
      setPredictLoading(false);
    }
  };

  // -- Actions : prédiction batch CSV --------------------------------------
  const handleBatchSubmit = async (e) => {
    e.preventDefault();
    if (!batchFile) {
      setBatchError("Sélectionnez un fichier CSV.");
      return;
    }
    setBatchLoading(true);
    setBatchError(null);
    setBatchResults(null);
    try {
      if (batchFormat === "csv") {
        const blob = await clientRef.current.predictBatchCsv({
          file: batchFile,
          textColumn: batchColumn,
        });
        const url = window.URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = "predictions.csv";
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.URL.revokeObjectURL(url);
        pushLog("success", "predictions.csv téléchargé.");
      } else {
        const { results } = await clientRef.current.predictBatchJson({
          file: batchFile,
          textColumn: batchColumn,
        });
        setBatchResults(results);
        addToHistory(results);
      }
    } catch (err) {
      setBatchError(err.message);
    } finally {
      setBatchLoading(false);
    }
  };

  // -- Actions : entraînement ------------------------------------------------
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
      const job = await clientRef.current.startTraining(payload);
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
      const job = await clientRef.current.cancelTraining(currentJob.job_id);
      setCurrentJob(job);
      pushLog("warning", `Annulation demandée pour ${job.job_id.slice(0, 8)}`);
    } catch (err) {
      pushLog("error", `Échec de l'annulation : ${err.message}`);
    }
  };

  const handleReloadPredictor = async () => {
    try {
      await clientRef.current.reloadPredictor(activeModel || undefined);
      pushLog("success", "Predictor rechargé depuis le disque.");
    } catch (err) {
      pushLog("error", `Échec du rechargement : ${err.message}`);
    }
  };

  const saveConfig = (e) => {
    e.preventDefault();
    setConfig(configDraft);
    pushLog("info", `Configuration mise à jour → ${configDraft.baseUrl}`);
  };

  const addToHistory = useCallback(
    (newPredictions) => {
      setPredictionsHistory((prev) => {
        const updated = [
          ...newPredictions.map((pred) => ({
            ...pred,
            timestamp: Date.now(),
          })),
          ...prev,
        ].slice(0, maxHistorySize);
        saveHistoryToStorage(updated);
        return updated;
      });
    },
    [maxHistorySize]
  );

  const clearHistory = useCallback(() => {
    setPredictionsHistory([]);
    saveHistoryToStorage([]);
    pushLog("info", "Historique des prédictions effacé.");
  }, [pushLog]);

  const setMaxHistorySize = useCallback((size) => {
    const newSize = Math.max(1, Math.min(1000, numOrUndef(size) || DEFAULT_MAX_HISTORY));
    setMaxHistorySizeState(newSize);
    saveSizeToStorage(newSize);
    setPredictionsHistory((prev) => prev.slice(0, newSize));
    saveHistoryToStorage(predictionsHistory.slice(0, newSize));
  }, [predictionsHistory]);

  const currentStepIndex = currentJob
    ? TRAIN_STEPS.indexOf(currentJob.step) === -1
      ? currentJob.status === "cancelled"
        ? TRAIN_STEPS.length
        : 0
      : TRAIN_STEPS.indexOf(currentJob.step)
    : -1;

  const healthDotClass = healthError
    ? "tt-dot tt-dot-red"
    : health?.model_available
    ? "tt-dot tt-dot-green"
    : "tt-dot tt-dot-amber";

  return (
    <div className="tt-root">
      <style>{CSS}</style>

      <header className="tt-header">
        <div className="tt-brand">
          <span className="tt-brand-mark">TT</span>
          <div>
            <h1>
              ThinkTuning <span className="tt-brand-sub">console</span>
            </h1>
            <p className="tt-brand-tag">
              Analyse de sentiments FR/EN — supervision faible, entraînement, inférence
            </p>
          </div>
        </div>

        <div className="tt-health">
          <span className={healthDotClass} />
          {healthError ? (
            <span className="tt-health-text" title={healthError}>
              API injoignable
            </span>
          ) : health ? (
            <span className="tt-health-text">
              {health.model_available ? "Modèle chargé" : "Aucun modèle"} · {health.active_jobs} job(s) actif(s)
            </span>
          ) : (
            <span className="tt-health-text">Connexion…</span>
          )}
        </div>
      </header>

      <details className="tt-settings">
        <summary>Connexion API ({config.baseUrl})</summary>
        <form className="tt-settings-form" onSubmit={saveConfig}>
          <label>
            URL de base
            <input
              type="text"
              value={configDraft.baseUrl}
              onChange={(e) => setConfigDraft((c) => ({ ...c, baseUrl: e.target.value }))}
              placeholder="http://localhost:8000"
            />
          </label>
          <label>
            Clé API (X-API-Key)
            <input
              type="password"
              value={configDraft.apiKey}
              onChange={(e) => setConfigDraft((c) => ({ ...c, apiKey: e.target.value }))}
              placeholder="API_KEY côté serveur"
            />
          </label>
          <button type="submit" className="tt-btn tt-btn-primary">
            Enregistrer
          </button>
        </form>
        {!config.apiKey && (
          <p className="tt-hint">
            Sans clé API, /health reste consultable mais toutes les autres routes (modèles,
            prédiction, entraînement) répondront 401.
          </p>
        )}
        {modelsError && <p className="tt-hint tt-hint-error">Modèles : {modelsError}</p>}
      </details>

      <main className="tt-grid">
        {/* --- Modèles ------------------------------------------------- */}
        <section className="tt-panel">
          <div className="tt-panel-head">
            <h2>Versions du modèle</h2>
            <button className="tt-btn tt-btn-ghost" onClick={refreshModels} type="button">
              Rafraîchir
            </button>
          </div>
          <div className="tt-model-list">
            <label className="tt-model-row">
              <input
                type="radio"
                name="active-model"
                checked={activeModel === ""}
                onChange={() => setActiveModel("")}
              />
              <span>Le plus récent (auto)</span>
            </label>
            {models.map((m) => (
              <label className="tt-model-row" key={m.path}>
                <input
                  type="radio"
                  name="active-model"
                  checked={activeModel === m.name}
                  onChange={() => setActiveModel(m.name)}
                />
                <span className="tt-mono">{m.name}</span>
                {m.active && <span className="tt-tag tt-tag-active">actif</span>}
                <span className="tt-model-date">{formatEpoch(m.created_at)}</span>
              </label>
            ))}
            {!models.length && <p className="tt-hint">Aucun modèle entraîné pour le moment.</p>}
          </div>
          <button className="tt-btn tt-btn-ghost" onClick={handleReloadPredictor} type="button">
            Forcer le rechargement du predictor
          </button>
        </section>

        {/* --- Prédiction unitaire --------------------------------------- */}
        <section className="tt-panel">
          <div className="tt-panel-head">
            <h2>Prédiction</h2>
          </div>
          <form onSubmit={handlePredict} className="tt-form">
            <textarea
              rows={5}
              value={predictText}
              onChange={(e) => setPredictText(e.target.value)}
              placeholder="Un texte par ligne…"
            />
            <button className="tt-btn tt-btn-primary" type="submit" disabled={predictLoading}>
              {predictLoading ? "Prédiction…" : "Prédire"}
            </button>
          </form>
          {predictError && <p className="tt-hint tt-hint-error">{predictError}</p>}
          {predictResults && (
            <ul className="tt-results">
              {predictResults.map((r, i) => (
                <li key={i} className="tt-result-row">
                  <span className={`tt-badge tt-badge-${r.sentiment}`}>
                    {SENTIMENT_LABELS[r.sentiment] || r.sentiment}
                  </span>
                  <span className="tt-result-text">{r.text}</span>
                  <span className="tt-confidence" title={`${(r.confidence * 100).toFixed(1)}%`}>
                    <span
                      className={`tt-confidence-fill tt-fill-${r.sentiment}`}
                      style={{ width: `${Math.round(r.confidence * 100)}%` }}
                    />
                  </span>
                </li>
              ))}
            </ul>
          )}

          <hr className="tt-divider" />

          <h3 className="tt-subtitle">Prédiction par lot (CSV)</h3>
          <form onSubmit={handleBatchSubmit} className="tt-form tt-form-inline">
            <input
              type="file"
              accept=".csv"
              onChange={(e) => setBatchFile(e.target.files?.[0] || null)}
            />
            <input
              type="text"
              value={batchColumn}
              onChange={(e) => setBatchColumn(e.target.value)}
              placeholder="colonne texte"
              className="tt-input-small"
            />
            <select value={batchFormat} onChange={(e) => setBatchFormat(e.target.value)}>
              <option value="json">Aperçu JSON</option>
              <option value="csv">Télécharger CSV</option>
            </select>
            <button className="tt-btn tt-btn-primary" type="submit" disabled={batchLoading}>
              {batchLoading ? "Envoi…" : "Lancer"}
            </button>
          </form>
          {batchError && <p className="tt-hint tt-hint-error">{batchError}</p>}
          {batchResults && (
            <table className="tt-table">
              <thead>
                <tr>
                  <th>Texte</th>
                  <th>Sentiment</th>
                  <th>Confiance</th>
                </tr>
              </thead>
              <tbody>
                {batchResults.slice(0, 20).map((r, i) => (
                  <tr key={i}>
                    <td className="tt-td-text">{r.text}</td>
                    <td>
                      <span className={`tt-badge tt-badge-${r.sentiment}`}>
                        {SENTIMENT_LABELS[r.sentiment] || r.sentiment}
                      </span>
                    </td>
                    <td className="tt-mono">{(r.confidence * 100).toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {batchResults && batchResults.length > 20 && (
            <p className="tt-hint">{batchResults.length - 20} lignes supplémentaires non affichées.</p>
          )}
        </section>

        {/* --- Historique des prédictions ---------------------------------- */}
        <section className="tt-panel tt-panel-wide">
          <div className="tt-panel-head">
            <h2>Historique des prédictions</h2>
            <div className="tt-history-controls">
              <input
                type="number"
                min="1"
                max="1000"
                value={maxHistorySize}
                onChange={(e) => setMaxHistorySize(e.target.value)}
                className="tt-input-small"
                title="Nombre maximum de prédictions à conserver"
              />
              <button className="tt-btn tt-btn-ghost" onClick={clearHistory} type="button">
                Effacer
              </button>
            </div>
          </div>
          {predictionsHistory.length === 0 ? (
            <p className="tt-hint">Aucune prédiction pour le moment.</p>
          ) : (
            <table className="tt-table tt-history-table">
              <thead>
                <tr>
                  <th>Date/Heure</th>
                  <th>Texte</th>
                  <th>Sentiment</th>
                  <th>Confiance</th>
                </tr>
              </thead>
              <tbody>
                {predictionsHistory.slice(0, 50).map((pred, idx) => (
                  <tr key={idx}>
                    <td className="tt-mono tt-history-time">
                      {new Date(pred.timestamp).toLocaleString()}
                    </td>
                    <td className="tt-history-text">{pred.text}</td>
                    <td>
                      <span className={`tt-badge tt-badge-${pred.sentiment}`}>
                        {SENTIMENT_LABELS[pred.sentiment] || pred.sentiment}
                      </span>
                    </td>
                    <td className="tt-mono">{(pred.confidence * 100).toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {predictionsHistory.length > 50 && (
            <p className="tt-hint">
              {predictionsHistory.length - 50} prédictions supplémentaires non affichées.
              (Total dans l'historique: {predictionsHistory.length})
            </p>
          )}
          {predictionsHistory.length > 0 && (
            <p className="tt-hint" style={{ marginTop: "8px" }}>
              Affichage des {Math.min(50, predictionsHistory.length)} dernières prédictions sur {predictionsHistory.length} conservées.
            </p>
          )}
        </section>

        {/* --- Entraînement ------------------------------------------------ */}
        <section className="tt-panel tt-panel-wide">
          <div className="tt-panel-head">
            <h2>Entraînement</h2>
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
                  onChange={(e) =>
                    setTrainForm((f) => ({ ...f, variants_per_example: e.target.value }))
                  }
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

            <button
              type="button"
              className="tt-btn tt-btn-ghost tt-advanced-toggle"
              onClick={() => setAdvancedOpen((v) => !v)}
            >
              {advancedOpen ? "Masquer" : "Afficher"} les overrides avancés (sinon configs/default.yaml)
            </button>

            {advancedOpen && (
              <div className="tt-field-grid">
                {[
                  ["epochs", "epochs"],
                  ["batch_size", "batch_size"],
                  ["num_workers", "num_workers"],
                  ["max_length", "max_length"],
                  ["learning_rate", "learning_rate"],
                  ["weight_decay", "weight_decay"],
                  ["warmup_ratio", "warmup_ratio"],
                ].map(([key, label]) => (
                  <label key={key}>
                    {label}
                    <input
                      type="number"
                      step="any"
                      value={trainForm[key]}
                      placeholder="config par défaut"
                      onChange={(e) => setTrainForm((f) => ({ ...f, [key]: e.target.value }))}
                    />
                  </label>
                ))}
              </div>
            )}

            <div className="tt-train-actions">
              <button className="tt-btn tt-btn-primary" type="submit" disabled={trainLoading}>
                {trainLoading ? "Démarrage…" : "Démarrer l'entraînement"}
              </button>
              {currentJob && !["completed", "failed", "cancelled"].includes(currentJob.status) && (
                <button
                  className="tt-btn tt-btn-danger"
                  type="button"
                  onClick={handleCancelTraining}
                >
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

          <table className="tt-table tt-jobs-table">
            <thead>
              <tr>
                <th>Job</th>
                <th>Statut</th>
                <th>Étape</th>
                <th>Démarré</th>
                <th>Terminé</th>
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
      </main>

      <footer className="tt-logs">
        <h2>Activité</h2>
        <ul>
          {logs.map((l) => (
            <li key={l.id} className={`tt-log-${l.type}`}>
              <span className="tt-mono">{new Date(l.ts).toLocaleTimeString()}</span> {l.text}
            </li>
          ))}
          {!logs.length && <li className="tt-hint">Aucun évènement pour le moment.</li>}
        </ul>
      </footer>
    </div>
  );
}

const CSS = `
:root {
  --tt-bg: #0b0e11;
  --tt-panel: #12161c;
  --tt-panel-border: #1e242c;
  --tt-text: #e7eaee;
  --tt-text-dim: #8b94a3;
  --tt-accent: #5b8def;
  --tt-negative: #f2545b;
  --tt-neutral: #f2b705;
  --tt-positive: #2fbf71;
}
.tt-root {
  background: var(--tt-bg);
  color: var(--tt-text);
  font-family: "Space Grotesk", system-ui, -apple-system, sans-serif;
  padding: 24px;
  border-radius: 12px;
  max-width: 1200px;
  margin: 0 auto;
}
.tt-mono {
  font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.85em;
}
.tt-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  border-bottom: 1px solid var(--tt-panel-border);
  padding-bottom: 16px;
  margin-bottom: 16px;
}
.tt-brand { display: flex; align-items: center; gap: 12px; }
.tt-brand-mark {
  display: inline-flex; align-items: center; justify-content: center;
  width: 40px; height: 40px; border-radius: 10px;
  background: linear-gradient(135deg, var(--tt-accent), var(--tt-positive));
  color: #0b0e11; font-weight: 700; font-family: "IBM Plex Mono", monospace;
}
.tt-brand h1 { margin: 0; font-size: 1.3rem; font-weight: 600; }
.tt-brand-sub { color: var(--tt-text-dim); font-weight: 400; }
.tt-brand-tag { margin: 2px 0 0; color: var(--tt-text-dim); font-size: 0.85rem; }
.tt-health { display: flex; align-items: center; gap: 8px; font-size: 0.85rem; }
.tt-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.tt-dot-green { background: var(--tt-positive); box-shadow: 0 0 8px var(--tt-positive); }
.tt-dot-amber { background: var(--tt-neutral); box-shadow: 0 0 8px var(--tt-neutral); }
.tt-dot-red { background: var(--tt-negative); box-shadow: 0 0 8px var(--tt-negative); }
.tt-health-text { color: var(--tt-text-dim); }

.tt-settings {
  background: var(--tt-panel); border: 1px solid var(--tt-panel-border);
  border-radius: 10px; padding: 12px 16px; margin-bottom: 20px;
}
.tt-settings summary { cursor: pointer; color: var(--tt-text-dim); font-size: 0.9rem; }
.tt-settings-form { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 12px; align-items: end; }
.tt-settings-form label { display: flex; flex-direction: column; gap: 4px; font-size: 0.8rem; color: var(--tt-text-dim); flex: 1; min-width: 220px; }

.tt-grid {
  display: grid;
  grid-template-columns: 1fr 1.3fr;
  gap: 16px;
}
.tt-panel-wide { grid-column: 1 / -1; }
@media (max-width: 860px) { .tt-grid { grid-template-columns: 1fr; } }

.tt-panel {
  background: var(--tt-panel); border: 1px solid var(--tt-panel-border);
  border-radius: 12px; padding: 18px;
}
.tt-panel-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.tt-panel h2 { margin: 0; font-size: 1rem; font-weight: 600; }
.tt-subtitle { font-size: 0.85rem; color: var(--tt-text-dim); margin: 12px 0 8px; }

input, select, textarea {
  background: #0d1117; border: 1px solid var(--tt-panel-border); color: var(--tt-text);
  border-radius: 8px; padding: 8px 10px; font-family: inherit; font-size: 0.9rem;
}
textarea { width: 100%; resize: vertical; }
.tt-input-small { width: 130px; }

.tt-btn {
  border: none; border-radius: 8px; padding: 8px 14px; font-size: 0.85rem;
  cursor: pointer; font-weight: 600; transition: opacity 0.15s ease;
}
.tt-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.tt-btn-primary { background: var(--tt-accent); color: #0b0e11; }
.tt-btn-primary:hover:not(:disabled) { opacity: 0.85; }
.tt-btn-ghost { background: transparent; border: 1px solid var(--tt-panel-border); color: var(--tt-text-dim); }
.tt-btn-ghost:hover { color: var(--tt-text); }
.tt-btn-danger { background: var(--tt-negative); color: #0b0e11; }

.tt-form { display: flex; flex-direction: column; gap: 10px; }
.tt-form-inline { flex-direction: row; flex-wrap: wrap; align-items: center; }
.tt-hint { color: var(--tt-text-dim); font-size: 0.8rem; margin: 6px 0 0; }
.tt-hint-error { color: var(--tt-negative); }
.tt-divider { border: none; border-top: 1px solid var(--tt-panel-border); margin: 16px 0; }

.tt-model-list { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
.tt-model-row { display: flex; align-items: center; gap: 8px; font-size: 0.85rem; padding: 4px 0; }
.tt-model-date { margin-left: auto; color: var(--tt-text-dim); font-size: 0.75rem; }

.tt-tag {
  font-size: 0.7rem; padding: 2px 8px; border-radius: 999px; text-transform: uppercase;
  letter-spacing: 0.03em; background: rgba(255,255,255,0.06); color: var(--tt-text-dim);
}
.tt-tag-active { background: rgba(47,191,113,0.15); color: var(--tt-positive); }
.tt-tag-status-completed { background: rgba(47,191,113,0.15); color: var(--tt-positive); }
.tt-tag-status-running { background: rgba(91,141,239,0.18); color: var(--tt-accent); }
.tt-tag-status-pending { background: rgba(242,183,5,0.15); color: var(--tt-neutral); }
.tt-tag-status-failed { background: rgba(242,84,91,0.15); color: var(--tt-negative); }
.tt-tag-status-cancelled { background: rgba(139,148,163,0.18); color: var(--tt-text-dim); }

.tt-results { list-style: none; margin: 12px 0 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.tt-result-row { display: grid; grid-template-columns: 90px 1fr 90px; align-items: center; gap: 10px; }
.tt-result-text { font-size: 0.85rem; }
.tt-confidence { display: block; height: 6px; background: #0d1117; border-radius: 4px; overflow: hidden; }
.tt-confidence-fill { display: block; height: 100%; }

.tt-badge { font-size: 0.7rem; padding: 3px 8px; border-radius: 999px; text-transform: uppercase; font-weight: 700; text-align: center; }
.tt-badge-negative, .tt-fill-negative { background: rgba(242,84,91,0.18); color: var(--tt-negative); }
.tt-badge-neutral, .tt-fill-neutral { background: rgba(242,183,5,0.18); color: var(--tt-neutral); }
.tt-badge-positive, .tt-fill-positive { background: rgba(47,191,113,0.18); color: var(--tt-positive); }
.tt-fill-negative { background: var(--tt-negative); }
.tt-fill-neutral { background: var(--tt-neutral); }
.tt-fill-positive { background: var(--tt-positive); }

.tt-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 0.82rem; }
.tt-table th, .tt-table td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--tt-panel-border); }
.tt-table th { color: var(--tt-text-dim); font-weight: 500; }
.tt-td-text { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.tt-field-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
.tt-field-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 0.75rem; color: var(--tt-text-dim); }
.tt-advanced-toggle { align-self: flex-start; }
.tt-train-actions { display: flex; gap: 10px; margin-top: 4px; }

.tt-job-live { margin-top: 16px; border-top: 1px solid var(--tt-panel-border); padding-top: 14px; }
.tt-job-live-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.tt-job-error { margin-top: 8px; }

.tt-tracker { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 4px; }
.tt-tracker-step { display: flex; align-items: center; gap: 6px; font-size: 0.72rem; padding: 4px 8px; border-radius: 999px; border: 1px solid var(--tt-panel-border); color: var(--tt-text-dim); }
.tt-tracker-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--tt-panel-border); }
.tt-tracker-done { color: var(--tt-positive); border-color: rgba(47,191,113,0.35); }
.tt-tracker-done .tt-tracker-dot { background: var(--tt-positive); }
.tt-tracker-active { color: var(--tt-accent); border-color: rgba(91,141,239,0.5); }
.tt-tracker-active .tt-tracker-dot { background: var(--tt-accent); box-shadow: 0 0 6px var(--tt-accent); }
.tt-tracker-error { color: var(--tt-negative); border-color: rgba(242,84,91,0.5); }
.tt-tracker-error .tt-tracker-dot { background: var(--tt-negative); }

.tt-history-controls { display: flex; gap: 8px; align-items: center; }
.tt-history-table { max-height: 400px; overflow-y: auto; }
.tt-history-time { white-space: nowrap; font-size: 0.75rem; }
.tt-history-text { max-width: 400px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.tt-logs { margin-top: 20px; border-top: 1px solid var(--tt-panel-border); padding-top: 14px; }
.tt-logs h2 { font-size: 0.9rem; margin: 0 0 8px; color: var(--tt-text-dim); }
.tt-logs ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; max-height: 180px; overflow-y: auto; }
.tt-logs li { font-size: 0.78rem; color: var(--tt-text-dim); }
.tt-log-error { color: var(--tt-negative); }
.tt-log-warning { color: var(--tt-neutral); }
.tt-log-success { color: var(--tt-positive); }
`;
