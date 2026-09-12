import type { FormEvent } from "react";
import type { DraftShape } from "./types";
import type { DraftUpdater } from "./useSettingsDraft";

interface Props {
  draft: DraftShape;
  updateDraft: DraftUpdater;
  configured: boolean;
  onSubmit: (event: FormEvent) => void;
}

export function ApiConnectionSection({ draft, updateDraft, configured, onSubmit }: Props) {
  return (
    <section className="tt-panel" aria-labelledby="settings-api-title">
      <div className="tt-panel-head">
        <h2 id="settings-api-title">Connexion API</h2>
        <span className={`tt-tag ${configured ? "tt-tag-status-completed" : "tt-tag-status-pending"}`}>
          {configured ? "configurée" : "manquante"}
        </span>
      </div>
      <form onSubmit={onSubmit} className="tt-form tt-settings-form-page">
        <label htmlFor="settings-base-url"><span className="tt-assistant-label">URL de base</span></label>
        <input id="settings-base-url" type="url" value={draft.baseUrl}
          onChange={(event) => updateDraft("baseUrl", event.target.value)} placeholder="http://localhost:8000" />
        <label htmlFor="settings-api-key"><span className="tt-assistant-label">Clé API (X-API-Key)</span></label>
        <input id="settings-api-key" type="password" value={draft.apiKey}
          onChange={(event) => updateDraft("apiKey", event.target.value)} placeholder="API_KEY côté serveur" />
        <button type="submit" className="tt-btn tt-btn-primary">Enregistrer</button>
      </form>
    </section>
  );
}
