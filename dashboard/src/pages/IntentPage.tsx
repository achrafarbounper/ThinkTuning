/**
 * Page « Classification d'intention » (chat / action).
 *
 * Interface du classifieur ``intent`` (Phase 5) : prédiction unitaire ou par
 * lot, repli transparent sur les règles métier quand aucun modèle n'est
 * entraîné, cache côté client (useIntentCache) et monitoring du classifieur
 * (engine, seuil, métriques, health) rafraîchi par polling.
 */

import { useCallback, useState, type FormEvent } from "react";
import { useApp } from "../context/useApp";
import {
  getIntentClassifierInfo,
  intentLabel,
  reloadIntentModel,
  type IntentClassifierInfo,
} from "../api/intentApi";
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
    null | { text: string; label: string; confidence: number }[]
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