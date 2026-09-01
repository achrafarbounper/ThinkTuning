/**
 * ModelSanityPanel — Santé & nettoyage du répertoire experiments/models.
 * ---------------------------------------------------------------------
 * Composant affiché dans la page « Entraînement » (SCRUM-74) :
 *   - liste toutes les versions de modèle (GET /models/details),
 *   - exécute le sanity check comportemental (GET /health/model-sanity?model_name=…)
 *     sur chaque version, bouton par modèle + « Analyser tout »,
 *   - permet de supprimer les versions défaillantes (DELETE /models/{name}) :
 *     le backend refuse (409) la version active et (422) tout modèle sain.
 *
 * Props :
 *   - client        : SentimentApiClient
 *   - models        : liste des versions [{ name, path, active, created_at }]
 *   - onModelsChanged : callback appelé après suppression
 *   - pushLog       : logger les événements
 */

import { useCallback, useState } from "react";

const VERDICT_META = {
  ok: { label: "Sain", className: "tt-sanity-verdict-ok" },
  untrained: { label: "Non entraîné", className: "tt-sanity-verdict-broken" },
  fallback_base_model: {
    label: "Fallback base model",
    className: "tt-sanity-verdict-broken",
  },
  model_unavailable: {
    label: "Illisible / incomplet",
    className: "tt-sanity-verdict-broken",
  },
  unknown: { label: "Non vérifié", className: "tt-sanity-verdict-unknown" },
  error: { label: "Erreur", className: "tt-sanity-verdict-unknown" },
};

const VERDICT_DELETEABLE = new Set([
  "untrained",
  "fallback_base_model",
  "model_unavailable",
]);

function verdictMeta(verdict) {
  return VERDICT_META[verdict] || VERDICT_META.unknown;
}

export default function ModelSanityPanel({ client, models = [], onModelsChanged, pushLog }) {
  const [reports, setReports] = useState({});
  const [checking, setChecking] = useState({});
  const [scanning, setScanning] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(null);

  const deleteOneSilent = useCallback(
    async (name) => {
      setChecking((prev) => ({ ...prev, [name]: true }));
      try {
        const res = await client.deleteModel(name);
        setReports((prev) => {
          const next = { ...prev };
          delete next[name];
          return next;
        });
        pushLog("success", "Version " + name + " supprimée (verdict : " + res.verdict + ").");
        return true;
      } catch (err) {
        setError(err.message);
        pushLog("error", "Suppression de " + name + " refusée : " + err.message);
        return false;
      } finally {
        setChecking((prev) => ({ ...prev, [name]: false }));
      }
    },
    [client, pushLog]
  );

  const checkOne = useCallback(
    async (name) => {
      setChecking((prev) => ({ ...prev, [name]: true }));
      try {
        const report = await client.getModelSanity(name);
        setReports((prev) => ({ ...prev, [name]: report }));
        pushLog(
          report.verdict === "ok" ? "success" : "error",
          "Sanity check " + name + " : " + report.verdict + "."
        );
        return report;
      } catch (err) {
        const report = { verdict: "error", detail: err.message };
        setReports((prev) => ({ ...prev, [name]: report }));
        pushLog("error", "Sanity check " + name + " échoué : " + err.message);
        return report;
      } finally {
        setChecking((prev) => ({ ...prev, [name]: false }));
      }
    },
    [client, pushLog]
  );

  const analyzeAll = useCallback(async () => {
    setScanning(true);
    setError(null);
    try {
      const names = models.map((m) => m.name);
      for (const name of names) {
        await checkOne(name);  
      }
      pushLog("info", "Analyse terminée : " + names.length + " version(s) vérifiée(s).");
    } finally {
      setScanning(false);
    }
  }, [models, checkOne, pushLog]);

  const deleteOne = useCallback(
    async (name) => {
      if (!window.confirm("Supprimer définitivement la version " + name + " ?")) return;
      const ok = await deleteOneSilent(name);
      if (ok) onModelsChanged?.();
    },
    [deleteOneSilent, onModelsChanged]
  );

  const cleanBroken = useCallback(async () => {
    const broken = models.filter(
      (m) => reports[m.name] && VERDICT_DELETEABLE.has(reports[m.name].verdict)
    );
    if (!broken.length) {
      pushLog("info", "Aucune version défaillante à supprimer.");
      return;
    }
    if (
      !window.confirm(
        "Supprimer " + broken.length + " version(s) défaillante(s) : " +
          broken.map((m) => m.name).join(", ") + " ?"
      )
    ) {
      return;
    }
    setCleaning(true);
    setError(null);
    try {
      let deleted = 0;
      for (const m of broken) {
        if (await deleteOneSilent(m.name)) deleted += 1;  
      }
      pushLog("info", "Nettoyage terminé : " + deleted + "/" + broken.length + " supprimée(s).");
      onModelsChanged?.();
    } finally {
      setCleaning(false);
    }
  }, [models, reports, deleteOneSilent, pushLog, onModelsChanged]);

  const brokenCount = models.filter(
    (m) => reports[m.name] && VERDICT_DELETEABLE.has(reports[m.name].verdict)
    ).length;

  if (!models.length) {
    return (
      <section className="tt-panel">
        <div className="tt-panel-head">
          <h2>Santé &amp; nettoyage des modèles</h2>
          <span className="tt-tag">experiments/models</span>
        </div>
        <p className="tt-hint">Aucune version de modèle : rien à analyser ni à nettoyer.</p>
      </section>
    );
  }

  return (
    <section className="tt-panel">
      <div className="tt-panel-head">
        <h2>Santé &amp; nettoyage des modèles</h2>
        <span className="tt-tag">{models.length} version(s)</span>
      </div>

      <p className="tt-hint">
        Le sanity check prédit sur un jeu de phrases FR/EN de référence pour
        détecter un modèle non entraîné ou en fallback. Les versions
        défaillantes peuvent être supprimées. Le modèle actif est jamais
        supprimable.
      </p>

      <div className="tt-sanity-actions">
        <button type="button" className="tt-btn tt-btn-ghost" onClick={analyzeAll} disabled={scanning || cleaning}>
          {scanning ? "Analyse en cours…" : "Analyser tout"}
        </button>
        <button
          type="button"
          className="tt-btn tt-btn-danger"
          onClick={cleanBroken}
          disabled={scanning || cleaning || brokenCount === 0}
          title={
            brokenCount > 0
              ? brokenCount + " version(s) défaillante(s) à supprimer"
              : "Analysez d'abord les versions"
          }
        >
          {cleaning
            ? "Nettoyage…"
            : "Nettoyer les modèles défaillants" + (brokenCount > 0 ? " (" + brokenCount + ")" : "")}
        </button>
      </div>

      {error && <div className="tt-alert tt-alert-error">{error}</div>}

      <table className="tt-table tt-sanity-table">
        <caption className="sr-only">Versions de modèles et résultat du sanity check</caption>
        <thead>
          <tr>
            <th scope="col">Version</th>
            <th scope="col">Statut</th>
            <th scope="col">Précision</th>
            <th scope="col">Actions</th>
          </tr>
        </thead>
        <tbody>
          {models.map((model) => {
            const report = reports[model.name];
            const meta = model.active
              ? { label: "Actif", className: "tt-sanity-verdict-active" }
              : verdictMeta(report?.verdict);
            const isChecking = !!checking[model.name];
            const isBroken = report && VERDICT_DELETEABLE.has(report.verdict);
            return (
              <tr key={model.name}>
                <td className="tt-mono">{model.name}</td>
                <td>
                  <span className={"tt-tag " + meta.className}>
                    {isChecking ? "Vérification…" : meta.label}
                  </span>
                  {report && report.verdict !== "ok" && (
                    <button
                      type="button"
                      className="tt-btn tt-btn-ghost tt-sanity-toggle"
                      onClick={() => setExpanded(expanded === model.name ? null : model.name)}
                    >
                      {expanded === model.name ? "Masquer" : "Détails"}
                    </button>
                  )}
                </td>
                <td className="tt-mono">
                  {report?.accuracy != null ? Math.round(report.accuracy * 100) + " %" : "—"}
                </td>
                <td>
                  <div className="tt-sanity-actions-row">
                    <button
                      type="button"
                      className="tt-btn tt-btn-ghost"
                      onClick={() => checkOne(model.name)}
                      disabled={isChecking || scanning || cleaning}
                    >
                      Vérifier
                    </button>
                    {isBroken && !model.active && (
                      <button
                        type="button"
                        className="tt-btn tt-btn-danger"
                        onClick={() => deleteOne(model.name)}
                        disabled={isChecking || scanning || cleaning}
                      >
                        Supprimer
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {models.map((model) => {
        if (expanded !== model.name) return null;
        const report = reports[model.name];
        if (!report) return null;
        const bad = (report.results || []).filter((r) => !r.correct);
        return (
          <div key={model.name + "-detail"} className="tt-sanity-detail">
            <p className="tt-hint">{report.detail}</p>
            {bad.length > 0 && (
              <table className="tt-table">
                <caption className="sr-only">Phrases mal classées par {model.name}</caption>
                <thead>
                  <tr>
                    <th scope="col">Phrase</th>
                    <th scope="col">Attendu</th>
                    <th scope="col">Prédit</th>
                    <th scope="col">Confiance</th>
                  </tr>
                </thead>
                <tbody>
                  {bad.map((r) => (
                    <tr key={r.text}>
                      <td>{r.text}</td>
                      <td>
                        <span className="tt-tag">{r.lang}</span> {r.expected}
                      </td>
                      <td>{r.predicted}</td>
                      <td className="tt-mono">{r.confidence.toFixed(3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        );
      })}
    </section>
  );
}
