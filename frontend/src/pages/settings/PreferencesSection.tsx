import { useState, type FormEvent } from "react";

interface Props {
  maxHistorySize: number;
  onSubmit: (value: string) => void;
}

export function PreferencesSection({ maxHistorySize, onSubmit }: Props) {
  const [draftSize, setDraftSize] = useState(String(maxHistorySize));

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    onSubmit(draftSize);
  };

  return (
    <section className="tt-panel" aria-labelledby="settings-preferences-title">
      <div className="tt-panel-head"><h2 id="settings-preferences-title">Préférences</h2></div>
      <form className="tt-form tt-settings-form-page" onSubmit={handleSubmit}>
        <label htmlFor="settings-history-size"><span className="tt-assistant-label">Prédictions conservées (max)</span></label>
        <input id="settings-history-size" type="number" min="1" max="1000"
          value={draftSize} onChange={(event) => setDraftSize(event.target.value)} className="tt-input-tt-settings" />
        <button type="submit" className="tt-btn tt-btn-ghost">Appliquer</button>
      </form>
      <p className="tt-hint">Historique stocké localement (localStorage).</p>
    </section>
  );
}
