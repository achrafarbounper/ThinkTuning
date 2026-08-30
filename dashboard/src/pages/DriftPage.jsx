/**
 * Page « Dérive » — détection de dérive entre deux batches de prédictions.
 *
 * Compare la distribution des sentiments prédits sur un batch de référence (A)
 * et un batch de production (B) via POST /drift. Deux modes d'entrée :
 * deux fichiers CSV, ou deux listes de textes. Paramètres : seuil d'alerte
 * et méthode de calcul (KL divergence ou test du khi-deux).
 */

import { useEffect, useState } from "react";
import { useApp } from "../context/useApp";
import ModelVersionSelector from "../components/ModelVersionSelector";

const SENTIMENT_LABELS = { negative: "négatif", neutral: "neutre", positive: "positif" };
const LABEL_ORDER = ["negative", "neutral", "positive"];
/** Aide contextuelle affichée sous le sélecteur de méthode. */
const METHOD_HINTS = {
  kl: "Divergence de Kullback-Leibler entre les distributions des batchs A et B (en nats). Sensible aux labels présents dans un batch et absents de l autre.",
  chi2: "Test du khi-deux : compare les effectifs du batch B aux proportions observées dans le batch A. La dérive se juge sur la p-value.",
};

function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
      <polyline points="22 4 12 14.01 9 11.01" />
    </svg>
  );
}

function WarnIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  );
}

/** Barre de distribution par label pour un batch (A ou B). */
function DistributionBar({ title, distribution, n }) {
  return (
    <section className="tt-panel">
      <div className="tt-panel-head">
        <h2>{title}</h2>
        <span className="tt-tag">{n} textes</span>
      </div>
      <div className="tt-drift-dist">
        {LABEL_ORDER.map((label) => {
          const pct = Math.round((distribution?.[label] ?? 0) * 100);
          return (
            <div key={label} className="tt-drift-dist-row">
              <span className="tt-drift-dist-label">
                {SENTIMENT_LABELS[label] || label}
              </span>
              <span className="tt-confidence" title={`${pct}%`}>
                <span
                  className={`tt-confidence-fill tt-fill-${label}`}
                  style={{ width: `${pct}%` }}
                />
              </span>
              <span className="tt-mono tt-drift-dist-value">{pct}%</span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

const IDLE = { status: "idle" };
const LOADING = { status: "loading" };

export default function DriftPage() {
  const { client, models, modelsError, refreshModels } = useApp();

  const [mode, setMode] = useState("csv"); // "csv" | "textes"
  const [fileA, setFileA] = useState(null);
  const [fileB, setFileB] = useState(null);
  const [textColumn, setTextColumn] = useState("text");
  const [textsA, setTextsA] = useState("");
  const [textsB, setTextsB] = useState("");
  const [threshold, setThreshold] = useState("0.1");
  const [method, setMethod] = useState("kl");
  const [model, setModel] = useState("");
  const [result, setResult] = useState(null);
  const [state, setState] = useState(IDLE);

  useEffect(() => {
    if (!models.length && !modelsError) refreshModels();
  }, [models.length, modelsError, refreshModels]);

  const textsAList = textsA.split("\n").map((t) => t.trim()).filter(Boolean);
  const textsBList = textsB.split("\n").map((t) => t.trim()).filter(Boolean);
  const thresholdNum = parseFloat(threshold);
  const thresholdValid = !Number.isNaN(thresholdNum) && thresholdNum > 0;

  const canSubmit =
    !state.loading &&
    thresholdValid &&
    (mode === "csv" ? Boolean(fileA && fileB) : textsAList.length > 0 && textsBList.length > 0);

  const handleAnalyze = async (e) => {
    e.preventDefault();
    if (!canSubmit) return;

    setState(LOADING);
    setResult(null);

    try {
      let data;
      if (mode === "csv") {
        data = await client.driftCsv({
          fileA,
          fileB,
          textColumn: textColumn.trim() || "text",
          threshold: thresholdNum,
          method,
          model: model || undefined,
        });
      } else {
        data = await client.driftTexts({
          textsA: textsAList,
          textsB: textsBList,
          threshold: thresholdNum,
          method,
          model: model || undefined,
        });
      }
      setResult(data);
      setState({ status: "done" });
    } catch (err) {
      setState({ status: "error", error: err?.message || "Erreur inconnue." });
    }
  };

  const verdict = result
    ? result.drift_detected
      ? "tt-compare-verdict--opposed"
      : "tt-compare-verdict--ok"
    : "";

  return (
    <>
      <header className="page-head">
        <h1>Dérive</h1>
        <p>
          Comparez la distribution des sentiments entre un batch de référence (A) et un batch de
          production (B) pour détecter automatiquement une dérive.
        </p>
      </header>

      <div className="page-body">
        <section className="tt-panel">
          <div className="tt-panel-head">
            <h2>Paramètres</h2>
            <span className="tt-tag">
              {method === "chi2" ? "Khi² (chi²)" : "KL divergence"} · seuil {threshold || "—"}
            </span>
          </div>

          <div className="tt-drift-settings">
            <div className="tt-drift-setting">
              <span className="tt-drift-setting-label">Modèle</span>
              <ModelVersionSelector
                models={models}
                activeModel={model}
                onModelChange={setModel}
                loading={!models.length}
                label=""
              />
              <span className="tt-drift-setting-hint">
                Version utilisée pour prédire les deux batchs.
              </span>
            </div>

            <div className="tt-drift-setting">
              <span className="tt-drift-setting-label">Méthode</span>
              <select value={method} onChange={(e) => setMethod(e.target.value)}>
                <option value="kl">KL divergence</option>
                <option value="chi2">Khi² (chi²)</option>
              </select>
              <span className="tt-drift-setting-hint">{METHOD_HINTS[method]}</span>
            </div>

            <div className="tt-drift-setting">
              <span className="tt-drift-setting-label">Seuil d'alerte</span>
              <input
                type="number"
                step="0.01"
                min="0.01"
                value={threshold}
                onChange={(e) => setThreshold(e.target.value)}
                aria-invalid={!thresholdValid}
              />
              <span
                className={`tt-drift-setting-hint${thresholdValid ? "" : " tt-hint-error"}`}
              >
                {thresholdValid
                  ? method === "chi2"
                    ? "Alerte si p-value < seuil (ex. 0.05)."
                    : "Alerte si score > seuil (en nats)."
                  : "Le seuil doit être un nombre > 0."}
              </span>
            </div>
          </div>
        </section>

        <section className="tt-panel">
          <div className="tt-panel-head">
            <h2>Batchs à comparer</h2>
            <div className="tt-drift-modes">
              <button
                type="button"
                className={`tt-btn tt-btn-ghost${mode === "csv" ? " tt-drift-mode--active" : ""}`}
                onClick={() => setMode("csv")}
              >
                Fichiers CSV
              </button>
              <button
                type="button"
                className={`tt-btn tt-btn-ghost${mode === "textes" ? " tt-drift-mode--active" : ""}`}
                onClick={() => setMode("textes")}
              >
                Listes de textes
              </button>
            </div>
          </div>

          <form className="tt-form" onSubmit={handleAnalyze}>
            {mode === "csv" ? (
              <>
                <div className="tt-compare-grid">
                  <label>
                    Batch A — référence (CSV)
                    <input
                      type="file"
                      accept=".csv,text/csv"
                      onChange={(e) => setFileA(e.target.files?.[0] || null)}
                    />
                  </label>
                  <label>
                    Batch B — production (CSV)
                    <input
                      type="file"
                      accept=".csv,text/csv"
                      onChange={(e) => setFileB(e.target.files?.[0] || null)}
                    />
                  </label>
                </div>
                <label>
                  Colonne de texte
                  <input
                    type="text"
                    value={textColumn}
                    onChange={(e) => setTextColumn(e.target.value)}
                    placeholder="text"
                  />
                </label>
              </>
            ) : (
              <div className="tt-compare-grid">
                <label>
                  Batch A — référence (un texte par ligne)
                  <textarea
                    rows={6}
                    value={textsA}
                    onChange={(e) => setTextsA(e.target.value)}
                    placeholder={"Premier texte…\nDeuxième texte…"}
                  />
                </label>
                <label>
                  Batch B — production (un texte par ligne)
                  <textarea
                    rows={6}
                    value={textsB}
                    onChange={(e) => setTextsB(e.target.value)}
                    placeholder={"Premier texte…\nDeuxième texte…"}
                  />
                </label>
              </div>
            )}

            <div className="tt-compare-actions">
              <button className="tt-btn tt-btn-primary" type="submit" disabled={!canSubmit}>
                {state.status === "loading" ? "Analyse…" : "Analyser la dérive"}
              </button>
              <span className="tt-hint">
                Les deux batchs sont prédits par le modèle, puis leurs distributions sont comparées.
              </span>
            </div>
          </form>

          {state.status === "error" && (
            <p className="tt-hint tt-hint-error">{state.error}</p>
          )}
        </section>

        {result && (
          <div className={`tt-compare-verdict ${verdict}`} role="status">
            {result.drift_detected ? <WarnIcon /> : <CheckIcon />}
            <span>
              {result.drift_detected
                ? `Dérive détectée — score ${result.drift_score} (seuil ${result.threshold})`
                : `Pas de dérive — score ${result.drift_score} sous le seuil ${result.threshold}`}
              {" · "}
              {result.method === "chi2"
                ? `p-value = ${result.p_value}`
                : `KL divergence = ${result.drift_score} nats`}
            </span>
          </div>
        )}

        {result && (
          <div className="tt-compare-grid">
            <DistributionBar title="Batch A — référence" distribution={result.distribution_a} n={result.n_a} />
            <DistributionBar title="Batch B — production" distribution={result.distribution_b} n={result.n_b} />
          </div>
        )}
      </div>
    </>
  );
}

