/**
 * Page « Analyse de sentiments » (FR/EN).
 *
 * Regroupe tout le volet inférence :
 * - sélection de la version du modèle (+ rechargement du predictor),
 * - prédiction unitaire (un texte par ligne),
 * - prédiction par lot (CSV, aperçu JSON ou téléchargement CSV),
 * - historique local des prédictions.
 */

import React, { useState } from "react";
import { useApp } from "../context/useApp";
import ModelVersionSelector from "../components/ModelVersionSelector";
import { sentimentLabel } from "../lib/sentiment";

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

  // Explication LLM d'une prédiction unitaire (POST /explain, OpenRouter).
  const [explainState, setExplainState] = useState(null);

  // Explication LLM d'une ligne de la prédiction par lot (POST /explain).
  const [batchExplainState, setBatchExplainState] = useState(null);

  // Explication LLM d'une ligne de l'historique (POST /explain).
  const [historyExplainState, setHistoryExplainState] = useState(null);

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
    setExplainState(null);
    setHistoryExplainState(null);
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

  // -- Explication LLM d'une prédiction unitaire ------------------------------
  const handleExplain = async (text) => {
    setExplainState({ text, loading: true, result: null, error: null });
    try {
      const result = await client.explain({ text });
      setExplainState({ text, loading: false, result, error: null });
    } catch (err) {
      setExplainState({ text, loading: false, result: null, error: err.message });
    }
  };

  // -- Explication LLM d'une ligne de prédiction par lot ----------------------
  const handleBatchExplain = async (index) => {
    const row = batchResults && batchResults[index];
    if (!row) return;
    setBatchExplainState({ index, loading: true, result: null, error: null });
    try {
      const result = await client.explain({ text: row.text });
      setBatchExplainState({ index, loading: false, result, error: null });
    } catch (err) {
      setBatchExplainState({ index, loading: false, result: null, error: err.message });
    }
  };

  // -- Explication LLM d'une ligne de l'historique -----------------------------
  const handleHistoryExplain = async (index) => {
    const pred = predictionsHistory && predictionsHistory[index];
    if (!pred) return;
    setHistoryExplainState({ index, loading: true, result: null, error: null });
    try {
      const result = await client.explain({ text: pred.text });
      setHistoryExplainState({ index, loading: false, result, error: null });
    } catch (err) {
      setHistoryExplainState({ index, loading: false, result: null, error: err.message });
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
    setBatchExplainState(null);
    setHistoryExplainState(null);
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
              {predictResults.map((r, i) => {
                const isExplaining = explainState?.text === r.text && explainState?.loading;
                const showExplain =
                  explainState?.text === r.text && !explainState?.loading;
                return (
                  <li key={i} className="tt-result-row">
                    <div className="tt-result-main">
                      <span className={`tt-badge tt-badge-${r.sentiment}`}>
                        {sentimentLabel(r.sentiment)}
                      </span>
                      <span className="tt-result-text">{r.text}</span>
                      <span className="tt-confidence" title={`${(r.confidence * 100).toFixed(1)}%`}>
                        <span
                          className={`tt-confidence-fill tt-fill-${r.sentiment}`}
                          style={{ width: `${Math.round(r.confidence * 100)}%` }}
                        />
                      </span>
                      <button
                        type="button"
                        className="tt-btn tt-btn-ghost tt-explain-btn"
                        onClick={() => handleExplain(r.text)}
                        disabled={isExplaining}
                        title="Expliquer cette prédiction via l'agent IA (OpenRouter)"
                      >
                        {isExplaining ? "Analyse…" : "Expliquer"}
                      </button>
                    </div>

                    {showExplain && (
                      <div className="tt-explain tt-explain-inline">
                        {explainState.error ? (
                          <p className="tt-hint tt-hint-error">{explainState.error}</p>
                        ) : (
                          <p>{explainState.result?.explanation}</p>
                        )}
                      </div>
                    )}
                  </li>
                );
              })}
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
              <caption className="sr-only">Résultats de prédiction par lot</caption>
              <thead>
                <tr>
                  <th scope="col">Texte</th>
                  <th scope="col">Sentiment</th>
                  <th scope="col">Confiance</th>
                  <th>Expliquer</th>
                </tr>
              </thead>
              <tbody>
                {batchResults.slice(0, 20).map((r, i) => {
                  const isBatchExplaining =
                    batchExplainState?.index === i && batchExplainState?.loading;
                  const showBatchExplain =
                    batchExplainState?.index === i && !batchExplainState?.loading;
                  return (
                    <React.Fragment key={i}>
                      <tr>
                        <td className="tt-td-text">{r.text}</td>
                        <td>
                          <span className={`tt-badge tt-badge-${r.sentiment}`}>
                            {sentimentLabel(r.sentiment)}
                          </span>
                        </td>
                        <td className="tt-mono">{(r.confidence * 100).toFixed(1)}%</td>
                        <td>
                          <button
                            type="button"
                            className="tt-btn tt-btn-ghost tt-explain-btn"
                            onClick={() => handleBatchExplain(i)}
                            disabled={isBatchExplaining}
                            title="Expliquer cette prédiction via l'agent IA (OpenRouter)"
                          >
                            {isBatchExplaining ? "Analyse…" : "Expliquer"}
                          </button>
                        </td>
                      </tr>
                      {showBatchExplain && (
                        <tr>
                          <td colSpan={4} className="tt-explain-cell">
                            {batchExplainState.error ? (
                              <span className="tt-hint tt-hint-error">
                                {batchExplainState.error}
                              </span>
                            ) : (
                              <span className="tt-explain tt-explain-inline">
                                {batchExplainState.result?.explanation}
                              </span>
                            )}
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })}
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
              <button className="tt-btn tt-btn-ghost" onClick={() => { clearHistory(); setHistoryExplainState(null); }} type="button">
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
              <caption className="sr-only">Historique des prédictions</caption>
              <thead>
                <tr>
                  <th scope="col">Date/Heure</th>
                  <th scope="col">Texte</th>
                  <th scope="col">Sentiment</th>
                  <th scope="col">Confiance</th>
                  <th scope="col">Expliquer</th>
                </tr>
              </thead>
              <tbody>
                {predictionsHistory.slice(0, 50).map((pred, idx) => (
                  <React.Fragment key={idx}>
                    <tr>
                      <td className="tt-mono tt-history-time">
                        {new Date(pred.timestamp).toLocaleString()}
                      </td>
                      <td className="tt-history-text">{pred.text}</td>
                      <td>
                        <span className={`tt-badge tt-badge-${pred.sentiment}`}>
                          {sentimentLabel(pred.sentiment)}
                        </span>
                      </td>
                      <td className="tt-mono">{(pred.confidence * 100).toFixed(1)}%</td>
                      <td>
                        <button
                          className="tt-btn tt-btn-ghost tt-explain-btn"
                          onClick={() => handleHistoryExplain(idx)}
                          disabled={historyExplainState?.index === idx && historyExplainState?.loading}
                          type="button"
                        >
                          {historyExplainState?.index === idx && historyExplainState?.loading ? "..." : "Expliquer"}
                        </button>
                      </td>
                    </tr>
                    {historyExplainState?.index === idx && (
                      <tr>
                        <td colSpan={5} className="tt-explain-cell">
                          {historyExplainState.error ? (
                            <span className="tt-hint tt-hint-error">
                              {historyExplainState.error}
                            </span>
                          ) : (
                            <span className="tt-explain tt-explain-inline">
                              {historyExplainState.result?.explanation}
                            </span>
                          )}
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
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
