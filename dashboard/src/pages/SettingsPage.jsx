/**
 * Page « Paramètres » — connexion à l'API FastAPI et préférences locales.
 *
 * La configuration (URL de base + clé X-API-Key) est persistée en localStorage
 * via AppContext ; elle est partagée par toutes les pages et par le chat.
 */

import { useState } from "react";
import { useApp } from "../context/useApp";

export default function SettingsPage() {
  const {
    config,
    setConfig,
    health,
    healthError,
    modelsError,
    maxHistorySize,
    setMaxHistorySize,
    clearHistory,
    predictionsHistory,
    pushLog,
  } = useApp();

  const [draft, setDraft] = useState(config);

  const saveConfig = (e) => {
    e.preventDefault();
    setConfig(draft);
    pushLog("info", `Configuration mise à jour → ${draft.baseUrl}`);
  };

  return (
    <>
      <header className="page-head">
        <h1>Paramètres</h1>
        <p>Connexion à l'API ThinkTuning et préférences du dashboard.</p>
      </header>

      <div className="page-body">
        <section className="tt-panel">
          <div className="tt-panel-head">
            <h2>Connexion API</h2>
            <span className={`tt-tag ${healthError ? "tt-tag-status-failed" : "tt-tag-status-completed"}`}>
              {healthError ? "injoignable" : health ? "en ligne" : "…"}
            </span>
          </div>
          <form onSubmit={saveConfig} className="tt-form tt-settings-form-page">
            <label>
              URL de base
              <input
                type="text"
                value={draft.baseUrl}
                onChange={(e) => setDraft((c) => ({ ...c, baseUrl: e.target.value }))}
                placeholder="http://localhost:8000"
              />
            </label>
            <label>
              Clé API (X-API-Key)
              <input
                type="password"
                value={draft.apiKey}
                onChange={(e) => setDraft((c) => ({ ...c, apiKey: e.target.value }))}
                placeholder="API_KEY côté serveur"
              />
            </label>
            <button type="submit" className="tt-btn tt-btn-primary">
              Enregistrer
            </button>
          </form>
          {!draft.apiKey && (
            <p className="tt-hint">
              Sans clé API, /health reste consultable mais toutes les autres routes
              (modèles, prédiction, entraînement) répondront 401.
            </p>
          )}
          {modelsError && <p className="tt-hint tt-hint-error">Modèles : {modelsError}</p>}
        </section>

        <section className="tt-panel">
          <div className="tt-panel-head">
            <h2>Préférences</h2>
          </div>
          <form
            className="tt-form tt-settings-form-page"
            onSubmit={(e) => {
              e.preventDefault();
              pushLog("info", `Historique limité à ${maxHistorySize} entrée(s).`);
            }}
          >
            <label>
              Prédictions conservées (max)
              <input
                type="number"
                min="1"
                max="1000"
                value={maxHistorySize}
                onChange={(e) => setMaxHistorySize(e.target.value)}
              />
            </label>
            <button type="submit" className="tt-btn tt-btn-ghost">
              Appliquer
            </button>
            <button type="button" className="tt-btn tt-btn-danger" onClick={clearHistory}>
              Effacer l'historique ({predictionsHistory.length})
            </button>
          </form>
          <p className="tt-hint">
            L'historique des prédictions est stocké localement (localStorage) et
            partagé avec la page Analyse de sentiments.
          </p>
        </section>

        <section className="tt-panel">
          <div className="tt-panel-head">
            <h2>À propos</h2>
          </div>
          <p className="tt-hint">
            ThinkTuning — pipeline complet de recomposition de données (EDA) +
            fine-tuning DistilBERT multilingue pour la classification de sentiments
            (positif / neutre / négatif) en français et en anglais. Assistant IA
            servi par Ollama via /api/ai.
          </p>
        </section>
      </div>
    </>
  );
}
