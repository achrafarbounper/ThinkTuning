import type { LlmModelInfo } from './types';

interface ChatModelSelectorProps {
  /** Modèles LLM disponibles côté serveur (GET /api/models). */
  models: LlmModelInfo[];
  /** Modèle actuellement sélectionné ('' = modèle par défaut du serveur). */
  selected: string;
  /** Appelé avec le nom du modèle choisi (ou '' pour le défaut serveur). */
  onChange: (modelName: string) => void;
  /** Vrai pendant le chargement initial de la liste. */
  loading?: boolean;
  /** Message d'erreur si la liste n'a pas pu être récupérée. */
  error?: string;
}

/**
 * Bouton de sélection du modèle LLM, affiché dans l'en-tête du chat.
 *
 * Rendu comme un <select> natif stylé en bouton (cohérent avec les autres
 * actions de l'en-tête) : accessible au clavier, sans dépendance externe.
 * La première option « Défaut » laisse le serveur utiliser son modèle
 * configuré (variables AGENT_*).
 */
export function ChatModelSelector({
  models,
  selected,
  onChange,
  loading = false,
  error = '',
}: ChatModelSelectorProps) {
  let title = 'Modèle IA utilisé par l\'assistant';
  if (error) {
    title = `Modèles indisponibles : ${error}`;
  } else if (loading) {
    title = 'Chargement des modèles disponibles…';
  } else if (models.length === 0) {
    title = 'Aucun modèle disponible (vérifiez que Ollama tourne)';
  }

  return (
    <select
      className="copilot-chat__model-select"
      data-error={error ? 'true' : undefined}
      value={selected}
      onChange={(event) => onChange(event.target.value)}
      disabled={loading || models.length === 0}
      title={title}
      aria-label="Sélection du modèle IA"
      aria-invalid={Boolean(error)}
    >
      <option value="">
        {loading ? 'Modèles…' : models.length > 0 ? 'Modèle : défaut' : 'Aucun modèle'}
      </option>
      {models.map((model) => (
        <option key={model.name} value={model.name}>
          {model.name}
          {model.is_default ? ' · défaut' : ''}
        </option>
      ))}
    </select>
  );
}