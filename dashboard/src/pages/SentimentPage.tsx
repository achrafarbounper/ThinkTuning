/**
 * Page « Analyse de sentiments » (FR/EN).
 *
 * Regroupe tout le volet inférence :
 * - sélection de la version du modèle (+ rechargement du predictor),
 * - prédiction unitaire (un texte par ligne),
 * - prédiction par lot (CSV, aperçu JSON ou téléchargement CSV),
 * - historique local des prédictions.
 */

import React, { useState, type FormEvent } from "react";
import { useApp } from "../context/useApp";
import type { PredictionResult } from "../api/sentimentApiClient";
import ModelVersionSelector from "../components/ModelVersionSelector";
import { useExplain } from "../hooks/useExplain";
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
  const [predictResults, setPredictResults] = useState<PredictionResult[] | null>(null);
  const [predictLoading, setPredictLoading] = useState(false);
  const [predictError, setPredictError] = useState<string | null>(null);

  // Explications LLM (POST /explain) : un hook unifié au lieu de 3 états dupliqués.
  const explainUnit = useExplain(client);
  const explainBatch = useExplain(client);
  const explainHistory = useExplain(client);

  const [batchFile, setBatchFile] = useState<File | null>(null);
  const [batchColumn, setBatchColumn] = useState("text");
  const [batchFormat, setBatchFormat] = useState("json");
  const [batchResults, setBatchResults] = useState<PredictionResult[] | null>(null);
  const [batchLoading, setBatchLoading] = useState(false);
  const [batchError, setBatchError] = useState<string | null>(null);

  // -- Prédiction unitaire ----------------------------------------------------
  const handlePredict = async (e: FormEvent) => {
    e.preventDefault();
    const texts = predictText
      .split("\n")
      .map((t) => t.trim())
      .filter(Boolean);
    if (!texts.length) return;

    setPredictLoading(true);
    setPredictError(null);
    explainUnit.reset();
    explainHistory.reset();
    try {
      const res = await client.predict(texts, activeModel || undefined);
      const results = res?.results ?? null;
      setPredictResults(results);
      if (results) addToHistory(results);
    } catch (err) {
      setPredictError(err instanceof Error ? err.message : String(err));
    } finally {
      setPredictLoading(false);
    }
  };

  // -- Explication LLM d'une prédiction unitaire ------------------------------
  const handleExplain = (text: string) => {
    void explainUnit.run(text);
  };

  // -- Explication LLM d'une ligne de prédiction par lot ----------------------
  const handleBatchExplain = (index: number) => {
    const row = batchResults && batchResults[index];
    if (!row) return;
    void explainBatch.run(row.text);
  };

  // -- Explication LLM d'une ligne de l'historique ----------------------------
  // On passe par le texte (identifiant stable) plutôt que l'index : les
  // nouvelles prédictions s'insèrent en tête de l'historique et décaleraient
  // les index (bug latent de l'ancienne version).
  const handleHistoryExplain = (index: number) => {
    const pred = predictionsHistory && predictionsHistory[index];
    if (!pred) return;
    void explainHistory.run(pred.text);
  };

  // -- Prédiction batch CSV -----------------------------------------------------
  const handleBatchSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!batchFile) {
      setBatchError("Sélectionnez un fichier CSV.");
      return;

    }
    setBatchLoading(true);
    setBatchError(null);
    setBatchResults(null);
    explainBatch.reset();
    explainHistory.reset();
    try {
      if (batchFormat === "csv") {
        const blob = (await client.predictBatchCsv({
          file: batchFile,
          textColumn: batchColumn,
        })) as Blob;
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
        const res = (await client.predictBatchJson({
          file: batchFile,
          textColumn: batchColumn,
        })) as { results?: PredictionResult[] };
        const results = res?.results ?? null;
        setBatchResults(results);
        if (results) addToHistory(results);
      }
    } catch (err) {
      setBatchError(err instanceof Error ? err.message : String(err));
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
      pushLog("error", `Échec du rechargement : ${err instanceof Error ? err.message : "?"}`);
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
                const isExplaining =
                  explainUnit.state.text === r.text && explainUnit.state.loading;
                const showExplain =
                  explainUnit.state.text === r.text && !explainUnit.state.loading;
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
                        {explainUnit.state.error ? (
                          <p className="tt-hint tt-hint-error">{explainUnit.state.error}</p>
                        ) : (
                          <p>{explainUnit.state.result}</p>
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
                    explainBatch.state.text === r.text && explainBatch.state.loading;
                  const showBatchExplain =
                    explainBatch.state.text === r.text && !explainBatch.state.loading;
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
                            {explainBatch.state.error ? (
                              <span className="tt-hint tt-hint-error">
                                {explainBatch.state.error}
                              </span>
                            ) : (
                              <span className="tt-explain tt-explain-inline">
                                {explainBatch.state.result}
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
              <button className="tt-btn tt-btn-ghost" onClick={() => { clearHistory(); explainHistory.reset(); }} type="button">
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
                        {pred.timestamp ? new Date(pred.timestamp).toLocaleString() : "—"}
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
                          disabled={explainHistory.state.text === pred.text && explainHistory.state.loading}
                          type="button"
                        >
                          {explainHistory.state.text === pred.text && explainHistory.state.loading ? "..." : "Expliquer"}
                        </button>
                      </td>
                    </tr>
                    {explainHistory.state.text === pred.text && !explainHistory.state.loading && (
                      <tr>
                        <td colSpan={5} className="tt-explain-cell">
                          {explainHistory.state.error ? (
                            <span className="tt-hint tt-hint-error">
                              {explainHistory.state.error}
                            </span>
                          ) : (
                            <span className="tt-explain tt-explain-inline">
                              {explainHistory.state.result}
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
