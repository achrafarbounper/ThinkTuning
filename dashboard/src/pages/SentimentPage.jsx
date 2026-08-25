/**
 * Page « Analyse de sentiments » (FR/EN).
 *
 * Regroupe tout le volet inférence :
 * - sélection de la version du modèle (+ rechargement du predictor),
 * - prédiction unitaire (un texte par ligne),
 * - prédiction par lot (CSV, aperçu JSON ou téléchargement CSV),
 * - historique local des prédictions.
 */

import { useState } from "react";
import { useApp } from "../context/useApp";
import ModelVersionSelector from "../components/ModelVersionSelector";

const SENTIMENT_LABELS = { negative: "négatif", neutral: "neutre", positive: "positif" };

export default function SentimentPage() {
  const {
    client,
    models,
    modelsError,
    activeModel,
    setActiveModel,
    predictionsHistory,
    addToHistory,
    clearHistory,
    maxHistorySize,
    setMaxHistorySize,
    pushLog,
  } = useApp();

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

  // -- Prédiction unitaire ----------------------------------------------------
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
      const { results } = await client.predict(texts, activeModel || undefined);
      setPredictResults(results);
      addToHistory(results);
    } catch (err) {
      setPredictError(err.message);
    } finally {
      setPredictLoading(false);
    }
  };

  // -- Prédiction batch CSV -----------------------------------------------------
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
        const blob = await client.predictBatchCsv({
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
        const { results } = await client.predictBatchJson({
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

  // -- Rechargement du predictor -------------------------------------------------
  const handleReloadPredictor = async () => {
    try {
      await client.reloadPredictor(activeModel || undefined);
      pushLog("success", "Predictor rechargé depuis le disque.");
    } catch (err) {
      pushLog("error", `Échec du rechargement : ${err.message}`);
    }
  };

  return (
    <>
      <header className="page-head">
        <h1>Analyse de sentiments</h1>
        <p>Classification positif / neutre / négatif — textes en français ou en anglais.</p>
      </header>

      <div className="page-body">
        <section className="tt-panel">
          <div className="tt-panel-head">
            <h2>Prédiction</h2>
            {modelsError && (
              <span className="tt-hint tt-hint-error">Modèles : {modelsError}</span>
            )}
          </div>

          <ModelVersionSelector
            models={models}
            activeModel={activeModel}
            onModelChange={setActiveModel}
            loading={!models || models.length === 0}
          />

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

        <section className="tt-panel">
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
              <button
                className="tt-btn tt-btn-ghost"
                onClick={handleReloadPredictor}
                type="button"
                title="Recharger le modèle actif depuis le disque"
              >
                Recharger le predictor
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
              (Total dans l'historique : {predictionsHistory.length})
            </p>
          )}
        </section>
      </div>
    </>
  );
}
