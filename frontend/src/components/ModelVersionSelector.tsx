/**
 * Affiche un sélecteur de version de modèle sous forme de dropdown.
 * Permet à l'utilisateur de choisir la version de modèle pour les prédictions.
 */

import type { ModelVersion } from "../api/sentimentApiClient";

export interface ModelVersionSelectorProps {
  models?: ModelVersion[];
  activeModel?: string;
  onModelChange?: (model: string) => void;
  loading?: boolean;
  label?: string;
}

export default function ModelVersionSelector({
  models = [],
  activeModel = "",
  onModelChange,
  loading = false,
  label = "Modèle pour les prédictions",
}: ModelVersionSelectorProps) {
  const handleChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    onModelChange?.(e.target.value);
  };

  if (loading) {
    return (
      <div className="tt-model-selector">
        <label className="tt-model-selector-label">
          {label}
          <select disabled className="tt-model-selector-select">
            <option>Chargement…</option>
          </select>
        </label>
      </div>
    );
  }

  return (
    <div className="tt-model-selector">
      <label className="tt-model-selector-label">
        {label}
        <select
          value={activeModel}
          onChange={handleChange}
          className="tt-model-selector-select"
        >
          <option value="">
            {models.length > 0 ? "Le plus récent (auto)" : "Aucun modèle disponible"}
          </option>
          {models.map((model) => (
            <option key={String(model.path ?? model.name)} value={model.name}>
              {model.name}
              {model.active ? " (actif)" : ""}
            </option>
          ))}
        </select>
      </label>
      {models.length > 0 && (
        <p className="tt-model-selector-hint">
          {activeModel
            ? `Modèle sélectionné : ${activeModel}`
            : "Utilisation du modèle le plus récent"}
        </p>
      )}
    </div>
  );
}
