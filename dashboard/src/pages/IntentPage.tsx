/**
 * Page « Classification d'intention » (chat / action).
 *
 * Interface du classifieur ``intent`` (Phase 5) : prédiction unitaire ou par
 * lot, repli transparent sur les règles métier quand aucun modèle n'est
 * entraîné, cache côté client (useIntentCache) et monitoring du classifieur
 * (engine, seuil, métriques, health) rafraîchi par polling.
 */

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useApp } from "../context/useApp";
import {
  getIntentClassifierInfo,
  intentLabel,
  reloadIntentModel,
  type IntentClassifierInfo,
} from "../api/intentApi";
import {
  activateIntentVersion,
  cancelIntentTraining,
  getIntentModelVersions,
  getIntentTrainingStatus,
  listIntentTrainingJobs,
  startIntentTraining,
  DEFAULT_INTENT_BASE_MODEL,
  DEFAULT_INTENT_DATASET,
  type IntentModelVersions,
  type IntentTrainPayload,
} from "../api/intentTrainApi";
import IntentTrainJobTracker from "../components/IntentTrainJobTracker";
import type { IntentTrainJob } from "../components/types";
import { useIntentCache } from "../hooks/useIntentCache";
import { usePolling } from "../hooks/usePolling";
import { formatDuration } from "../lib/format";

const SAMPLES = [
  "Peux-tu lancer l'entraînement du modèle ?",
  "Merci beaucoup pour ton aide",
  "Cherche le rapport du dernier trimestre",
  "Bonjour, comment ça va aujourd'hui ?",
];

export default function IntentPage() {
  const { client, pushLog } = useApp();
  const intentCache = useIntentCache({ maxEntries: 200 });

  const [texts, setTexts] = useState(SAMPLES.join("\n"));
  const [results, setResults] = useState<
    | null
    | {
        text: string;
        label: string;
        confidence: number;
        probabilities?: Record<string, number>;
      }[]
  >(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fromCache, setFromCache] = useState(false);

  const [info, setInfo] = useState<IntentClassifierInfo | null>(null);
  const [infoError, setInfoError] = useState<string | null>(null);
  const [reloading, setReloading] = useState(false);

  const refreshInfo = useCallback(async () => {
    try {
      const snap = await getIntentClassifierInfo(client);
      setInfo(snap);
      setInfoError(null);
    } catch (err) {
      setInfoError(err instanceof Error ? err.message : String(err));
    }
  }, [client]);

  usePolling({
    intervalMs: 15_000,
    immediate: true,
    initialDelayMs: 1_000,
    tick: refreshInfo,
  });

  const handlePredict = async (e: FormEvent) => {
    e.preventDefault();
    const parsed = texts
      .split("\n")
      .map((t) => t.trim())
      .filter(Boolean);
    if (!parsed.length) return;

    setLoading(true);
    setError(null);
    try {
      const outcome = await intentCache.predict(client, parsed);
      setResults(outcome.results);
      setFromCache(outcome.fromCache);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      pushLog("error", `Classification d'intention échouée : ${message}`);
    } finally {
      setLoading(false);
    }
  };

  const handleReload = async () => {
    setReloading(true);
    try {
      await reloadIntentModel(client);
      pushLog("info", "Classifieur d'intention rechargé.");
      await refreshInfo();
    } catch (err) {
      pushLog(
        "error",
        `Rechargement intention impossible : ${err instanceof Error ? err.message : String(err)}`
      );
    } finally {
      setReloading(false);
    }
  };

  // -- Entraînement du classifieur d'intention (SCRUM-95) --------------------
  const [trainForm, setTrainForm] = useState<IntentTrainPayload>({
    dataset_path: DEFAULT_INTENT_DATASET,
    base_model: DEFAULT_INTENT_BASE_MODEL,
    base_model_version: null,
    epochs: 3,
    batch_size: 32,
    learning_rate: 2e-5,
    max_length: 128,
    test_size: 0.1,
    quantize_int8: false,
    activate: false,
  });
  const [trainLoading, setTrainLoading] = useState(false);
  const [trainError, setTrainError] = useState<string | null>(null);
  const [currentJob, setCurrentJob] = useState<IntentTrainJob | null>(null);
  const [cancelLoading, setCancelLoading] = useState(false);
  const [jobs, setJobs] = useState<IntentTrainJob[]>([]);
  const [versions, setVersions] = useState<IntentModelVersions | null>(null);
  const [activating, setActivating] = useState(false);

  const refreshTrainingHistory = useCallback(async () => {
    try {
      const page = await listIntentTrainingJobs(client, { limit: 10 });
      setJobs(page?.items ?? []);
    } catch {
      // Historique non bloquant : la prédiction reste utilisable.
    }
  }, [client]);

  const refreshVersions = useCallback(async () => {
    try {
      setVersions(await getIntentModelVersions(client));
    } catch {
      setVersions(null);
    }
  }, [client]);

  // Chargement initial : versions (continual training) + historique des jobs.
  useEffect(() => {
    refreshVersions();
    refreshTrainingHistory();
  }, [refreshVersions, refreshTrainingHistory]);

  // Suivi du job en cours (poll 4 s, comme la page Entraînement).
  const jobId = currentJob?.job_id ?? null;
  const jobActive =
    currentJob?.status === "running" || currentJob?.status === "pending";
  useEffect(() => {
    if (!jobActive || !jobId) return undefined;
    const timer = setInterval(async () => {
      try {
        const snap = await getIntentTrainingStatus(client, jobId);
        if (!snap) return;
        setCurrentJob(snap);
        if (["completed", "failed", "cancelled"].includes(snap.status)) {
          pushLog(
            snap.status === "completed" ? "success" : "error",
            `Entraînement d'intention ${snap.status} — job ${snap.job_id.slice(0, 8)}`
          );
          refreshTrainingHistory();
          refreshVersions();
          refreshInfo(); // engine/métriques peuvent changer si activate=true
        }
      } catch {
        // Erreurs de polling tolérées : le tick suivant réessaie.
      }
    }, 4000);
    return () => clearInterval(timer);
  }, [
    jobActive,
    jobId,
    client,
    pushLog,
    refreshTrainingHistory,
    refreshVersions,
    refreshInfo,
  ]);

  const handleStartIntentTraining = async (e: FormEvent) => {
    e.preventDefault();
    setTrainError(null);
    setTrainLoading(true);
    const payload: IntentTrainPayload = {
      ...trainForm,
      base_model_version: trainForm.base_model_version || null,
    };
    try {
      const job = await startIntentTraining(client, payload);
      if (job) {
        setCurrentJob(job);
        pushLog(
          "info",
          `Entraînement d'intention lancé — job ${job.job_id.slice(0, 8)}` +
            (payload.base_model_version
              ? ` (continual depuis ${payload.base_model_version})`
              : "")
        );
      }
      refreshTrainingHistory();
    } catch (err) {
      setTrainError(err instanceof Error ? err.message : String(err));
    } finally {
      setTrainLoading(false);
    }
  };

  const handleCancelIntentTraining = async () => {
    if (!currentJob) return;
    setCancelLoading(true);
    try {
      const job = await cancelIntentTraining(client, currentJob.job_id);
      if (job) setCurrentJob(job);
      pushLog("info", "Annulation de l'entraînement d'intention demandée.");
    } catch (err) {
      pushLog(
        "error",
        `Échec de l'annulation : ${err instanceof Error ? err.message : String(err)}`
      );
    } finally {
      setCancelLoading(false);
    }
  };

  const handleActivateIntentVersion = async (version: string) => {
    setActivating(true);
    try {
      // Active (active.json) PUIS recharge le classifieur en mémoire.
      await activateIntentVersion(client, version);
      pushLog("success", `Version d'intention active : ${version} (classifieur rechargé).`);
      await Promise.all([refreshVersions(), refreshInfo()]);
    } catch (err) {
      pushLog(
        "error",
        `Activation impossible : ${err instanceof Error ? err.message : String(err)}`
      );
    } finally {
      setActivating(false);
    }
  };

  const stats = intentCache.stats();
  const engine = info?.info?.engine ?? "—";
  const labels = info?.info?.labels ?? [];

  return (
    <>
      <div className="tt-panel tt-panel-wide">
        <div className="tt-panel-head">
          <h1>Classification d'intention</h1>
          <span className="tt-hint">Chat vs action — restaurant du routage agent</span>
        </div>
        <p className="tt-hint">
          Détecte si un message est une simple <strong>discussion</strong> ou une demande{" "}
          <strong>d'action</strong> (lancer un outil, chercher, entraîner…). Sans modèle
          entraîné, le classifieur retombe sur les règles métier.
        </p>

        <form onSubmit={handlePredict} className="tt-form">
          <label className="tt-label" htmlFor="intent-texts">
            Messages (un par ligne)
          </label>
          <textarea
            id="intent-texts"
            value={texts}
            onChange={(e) => setTexts(e.target.value)}
            rows={5}
            className="tt-input"
          />
          <div className="tt-row" style={{ gap: 8, marginTop: 10 }}>
            <button className="tt-btn tt-btn-primary" disabled={loading} type="submit">
              {loading ? "Classification…" : "Classer"}
            </button>
            <button
              className="tt-btn tt-btn-ghost"
              type="button"
              onClick={() => intentCache.clear()}
              title="Vider le cache de prédictions du client"
            >
              Vider le cache
            </button>
          </div>
        </form>

        {error && <p className="tt-hint tt-hint-error">{error}</p>}

        {results && (
          <section className="tt-results-block" aria-label="Résultats">
            <h2>Résultats</h2>
            <table className="tt-table tt-history-table">
              <caption className="sr-only">Prédictions d'intention</caption>
              <thead>
                <tr>
                  <th scope="col">Message</th>
                  <th scope="col">Intention</th>
                  <th scope="col">Confiance</th>
                  <th scope="col">Distribution</th>
                </tr>
              </thead>
              <tbody>
                {results.map((r, i) => (
                  <tr key={`${r.text}-${i}`}>
                    <td>{r.text}</td>
                    <td>
                      <span
                        className={
                          r.label === "action"
                            ? "tt-badge tt-badge-positive"
                            : "tt-badge tt-badge-neutral"
                        }
                      >
                        {intentLabel(r.label)}
                      </span>
                    </td>
                    <td className="tt-mono">{(r.confidence * 100).toFixed(1)}%</td>
                    <td className="tt-mono">
                      {r.probabilities
                        ? Object.entries(r.probabilities)
                            .map(
                              ([label, p]) =>
                                `${intentLabel(label)} ${(p * 100).toFixed(1)}%`
                            )
                            .join(" · ")
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="tt-hint">
              {fromCache
                ? "Réponse servie depuis le cache local (textes déjà classés)."
                : "Réponse fraîche (les textes manquants ont été prédits puis mis en cache)."}
            </p>
          </section>
        )}
      </div>

  <div className="tt-panel">
        <div className="tt-panel-head">
          <h2>Monitoring du classifieur</h2>
          <button
            className="tt-btn tt-btn-ghost"
            onClick={handleReload}
            disabled={reloading}
            type="button"
          >
            {reloading ? "Rechargement…" : "Recharger le modèle"}
          </button>
        </div>
        {infoError && <p className="tt-hint tt-hint-error">{infoError}</p>}
        <dl className="tt-metrics-table">
          <div>
            <dt>Moteur</dt>
            <dd className="tt-mono">{engine}</dd>
          </div>
          <div>
            <dt>Labels</dt>
            <dd className="tt-mono">{labels.length ? labels.join(" / ") : "—"}</dd>
          </div>
          <div>
            <dt>Santé</dt>
            <dd className="tt-mono">
              {info?.health?.ok === true
                ? "ok"
                : info?.health?.ok === false
                ? "hors-service"
                : "—"}
            </dd>
          </div>
          <div>
            <dt>Prédictions</dt>
            <dd className="tt-mono">{info?.metrics?.predictions ?? 0}</dd>
          </div>
          <div>
            <dt>Erreurs</dt>
            <dd className="tt-mono">{info?.metrics?.errors ?? 0}</dd>
          </div>
        </dl>
        {info?.warmup && (
          <p className="tt-hint">
            Warmup :{" "}
            {info.warmup.ok === true
              ? "OK"
              : info.warmup.ok === false
              ? "échec"
              : "non tenté"}
            {typeof info.warmup.latency_ms === "number"
              ? ` (${formatDuration(info.warmup.latency_ms)})`
              : ""}
          </p>
        )}
      </div>

      <div className="tt-panel">
        <div className="tt-panel-head">
          <h2>Entraînement du classifieur d'intention</h2>
        </div>
        <p className="tt-hint">
          Fine-tune un encodeur multilingue sur un dataset JSONL{" "}
          <span className="tt-mono">{'{"text","label"}'}</span> (labels{" "}
          <span className="tt-mono">chat</span>/<span className="tt-mono">action</span>) et
          enregistre une version dans{" "}
          <span className="tt-mono">experiments/intent_models/</span>.
        </p>
        <form onSubmit={handleStartIntentTraining} className="tt-form">
          <div className="tt-field-grid">
            <label>
              dataset_path
              <input
                type="text"
                value={trainForm.dataset_path}
                onChange={(e) =>
                  setTrainForm((f) => ({ ...f, dataset_path: e.target.value }))
                }
              />
            </label>
            <label>
              epochs
              <input
                type="number"
                min="1"
                value={trainForm.epochs}
                onChange={(e) =>
                  setTrainForm((f) => ({ ...f, epochs: Number(e.target.value) }))
                }
              />
            </label>
            <label>
              batch_size
              <input
                type="number"
                min="1"
                value={trainForm.batch_size}
                onChange={(e) =>
                  setTrainForm((f) => ({ ...f, batch_size: Number(e.target.value) }))
                }
              />
            </label>
            <label>
              learning_rate
              <input
                type="number"
                step="0.00001"
                value={trainForm.learning_rate}
                onChange={(e) =>
                  setTrainForm((f) => ({ ...f, learning_rate: Number(e.target.value) }))
                }
              />
            </label>
            <label>
              max_length
              <input
                type="number"
                min="8"
                value={trainForm.max_length}
                onChange={(e) =>
                  setTrainForm((f) => ({ ...f, max_length: Number(e.target.value) }))
                }
              />
            </label>
            <label>
              Reprendre depuis
              <select
                value={trainForm.base_model_version ?? ""}
                onChange={(e) =>
                  setTrainForm((f) => ({
                    ...f,
                    base_model_version: e.target.value || null,
                  }))
                }
              >
                <option value="">Modèle de base (from scratch)</option>
                {(versions?.items ?? []).map((v) => (
                  <option key={v} value={v}>
                    {v}
                    {versions?.active === v ? " (active)" : ""}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="tt-row" style={{ gap: 16 }}>
            <label>
              <input
                type="checkbox"
                checked={trainForm.quantize_int8}
                onChange={(e) =>
                  setTrainForm((f) => ({ ...f, quantize_int8: e.target.checked }))
                }
              />{" "}
              Quantification INT8
            </label>
            <label>
              <input
                type="checkbox"
                checked={trainForm.activate}
                onChange={(e) =>
                  setTrainForm((f) => ({ ...f, activate: e.target.checked }))
                }
              />{" "}
              Activer la version produite
            </label>
          </div>
          <div className="tt-train-actions">
            <button
              className="tt-btn tt-btn-primary"
              type="submit"
              disabled={trainLoading}
            >
              {trainLoading ? "Lancement…" : "Lancer l'entraînement d'intention"}
            </button>
          </div>
        </form>
        {trainError && <p className="tt-hint tt-hint-error">{trainError}</p>}

        <IntentTrainJobTracker
          job={currentJob}
          onCancel={handleCancelIntentTraining}
          cancelLoading={cancelLoading}
        />

        <div className="tt-history-section">
          <h3 className="tt-subtitle">Historique des entraînements d'intention</h3>
          {jobs.length === 0 ? (
            <p className="tt-hint">Aucun job d'intention enregistré pour l'instant.</p>
          ) : (
            <table className="tt-table tt-history-table">
              <caption className="sr-only">Jobs d'entraînement d'intention</caption>
              <thead>
                <tr>
                  <th scope="col">Job</th>
                  <th scope="col">Statut</th>
                  <th scope="col">Étape</th>
                  <th scope="col">Début</th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.job_id}>
                    <td className="tt-mono">{j.job_id.slice(0, 8)}</td>
                    <td>{j.status}</td>
                    <td className="tt-mono">{j.step}</td>
                    <td className="tt-mono">
                      {j.started_at
                        ? new Date(j.started_at * 1000).toLocaleString()
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="tt-panel">
        <div className="tt-panel-head">
          <h2>Versions du modèle d'intention</h2>
          <button
            className="tt-btn tt-btn-ghost"
            type="button"
            onClick={refreshVersions}
          >
            Rafraîchir
          </button>
        </div>
        {!versions || versions.total === 0 ? (
          <p className="tt-hint">
            Aucun modèle d'intention entraîné — le classifieur utilise le repli de
            règles. Lancez un entraînement ci-dessus ou via{" "}
            <span className="tt-mono">scripts/train_intent.py</span>.
          </p>
        ) : (
          <table className="tt-table tt-history-table">
            <caption className="sr-only">Versions du modèle d'intention</caption>
            <thead>
              <tr>
                <th scope="col">Version</th>
                <th scope="col">Statut</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {versions.items.map((v) => (
                <tr key={v}>
                  <td className="tt-mono">{v}</td>
                  <td>
                    {versions.active === v ? (
                      <span className="tt-badge tt-badge-positive">active</span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>
                    {versions.active !== v && (
                      <button
                        className="tt-btn tt-btn-ghost"
                        type="button"
                        disabled={activating}
                        onClick={() => handleActivateIntentVersion(v)}
                      >
                        {activating ? "Activation…" : "Activer"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="tt-hint">
          L'activation met à jour le pointeur{" "}
          <span className="tt-mono">active.json</span> puis recharge le classifieur
          en mémoire (POST <span className="tt-mono">/classifiers/intent/reload</span>).
        </p>
      </div>

      <div className="tt-panel">
        <div className="tt-panel-head">
          <h2>Cache de prédictions (client)</h2>
          <button
            className="tt-btn tt-btn-ghost"
            onClick={() => intentCache.clear()}
            type="button"
          >
            Vider
          </button>
        </div>
        <dl className="tt-metrics-table">
          <div>
            <dt>Entrées</dt>
            <dd className="tt-mono">
              {stats.size} / {stats.maxEntries}
            </dd>
          </div>
          <div>
            <dt>Hit rate</dt>
            <dd className="tt-mono">{(stats.hitRate * 100).toFixed(1)}%</dd>
          </div>
          <div>
            <dt>Hits</dt>
            <dd className="tt-mono">{stats.hits}</dd>
          </div>
          <div>
            <dt>Misses</dt>
            <dd className="tt-mono">{stats.misses}</dd>
          </div>
        </dl>
      </div>
    </>
  );
}