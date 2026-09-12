import type { DraftShape, NumericDraftKey } from "./types";
import type { DraftUpdater } from "./useSettingsDraft";

interface Props {
  draft: DraftShape;
  updateDraft: DraftUpdater;
  loading: boolean;
  onSave: () => void;
}

const numericFields: Array<[NumericDraftKey, string, number, number, number?]> = [
  ["trainMaxPerLang", "Exemples par langue", 1, 100000],
  ["trainAugmentFraction", "Fraction augmentation", 0, 1, 0.05],
  ["trainVariantsPerExample", "Variantes par exemple", 1, 100],
  ["trainEpochs", "Epochs", 1, 100],
  ["trainBatchSize", "Batch size", 1, 1024],
  ["trainNumWorkers", "Workers", 0, 128],
  ["trainMaxLength", "Longueur maximale", 8, 4096],
  ["trainLearningRate", "Learning rate", 0.0000001, 1, 0.000001],
  ["trainWeightDecay", "Weight decay", 0, 1, 0.001],
  ["trainWarmupRatio", "Warmup ratio", 0, 1, 0.01],
];

function Field({ id, label, value, min, max, step, onChange }: {
  id: string; label: string; value: string | number; min: number; max: number; step?: number;
  onChange: (value: string) => void;
}) {
  return <label htmlFor={id}>
    <span className="tt-assistant-label">{label}</span>
    <input id={id} type="number" min={min} max={max} step={step} value={value}
      onChange={(event) => onChange(event.target.value)} className="tt-input-tt-settings" />
  </label>;
}

export function TrainingSettingsSection({ draft, updateDraft, loading, onSave }: Props) {
  const updateNumber = (key: NumericDraftKey) => (value: string) => updateDraft(key, Number(value));
  return <section className="tt-panel tt-settings-assistant-panel" aria-labelledby="settings-training-title">
    <div className="tt-panel-head"><h2 id="settings-training-title">Entraînement ML</h2>
      <span className="tt-tag tt-tag-status-completed">defaults</span></div>
    <p className="tt-assistant-section-help">Ces valeurs préremplissent les nouveaux jobs. Un override explicite dans la page Entraînement reste prioritaire.</p>
    <div className="tt-assistant-grid">
      {numericFields.map(([key, label, min, max, step]) => <Field key={key} id={`settings-${String(key)}`} label={label}
        min={min} max={max} step={step} value={draft[key] as number | string} onChange={updateNumber(key)} />)}
      <label className="tt-checkbox-label"><input type="checkbox" checked={Boolean(draft.trainUseBackTranslation)}
        onChange={(event) => updateDraft("trainUseBackTranslation", event.target.checked)} /><span>Activer la back-translation</span></label>
      <label htmlFor="settings-train-device"><span className="tt-assistant-label">Device</span>
        <select id="settings-train-device" value={draft.trainDevice} onChange={(event) => updateDraft("trainDevice", event.target.value)}
          className="tt-select-tt-settings"><option value="auto">Auto</option><option value="cpu">CPU</option><option value="cuda">CUDA</option></select>
      </label>
    </div>
    <div className="tt-assistant-actions">
      <button type="button" className="tt-btn tt-btn-primary" onClick={onSave} disabled={loading}>{loading ? "Enregistrement..." : "Enregistrer"}</button>
    </div>
  </section>;
}
