import { useState } from 'react';

interface ThinkingBlockProps {
  /** Trace de raisonnement accumulée (peut être vide au démarrage). */
  thinking: string;
  /** Vrai tant que la réflexion est encore en cours de diffusion. */
  streaming: boolean;
}

/**
 * Bloc repliable affichant la trace de raisonnement de l'assistant,
 * façon DeepSeek R1 : déplié automatiquement pendant la génération puis
 * refermé à la fin (réouverture manuelle possible à tout moment).
 *
 * Rendu uniquement pour les messages de l'assistant, AU-DESSUS de la bulle
 * de réponse ; disparaît si aucune réflexion n'a été produite.
 */
export function ThinkingBlock({ thinking, streaming }: ThinkingBlockProps) {
  const [expanded, setExpanded] = useState(streaming);

  // Dépliage pendant la diffusion de la réflexion, refermeture à la fin.
  // Ajustement d'état PENDANT le rendu (pattern React documenté, cf. Settings)
  // plutôt qu'un useEffect + setState qui déclenche des rendus en cascade.
  const [prevStreaming, setPrevStreaming] = useState(streaming);
  if (streaming !== prevStreaming) {
    setPrevStreaming(streaming);
    setExpanded(streaming);
  }

  // Rien à afficher et rien qui arrive : le bloc n'est pas rendu du tout.
  if (!streaming && thinking.trim().length === 0) return null;

  return (
    <div className="chat-thinking" data-streaming={streaming ? 'true' : undefined}>
      <button
        type="button"
        className="chat-thinking__toggle"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        aria-label={
          streaming
            ? 'Masquer la réflexion en cours'
            : 'Afficher ou masquer la réflexion de l’assistant'
        }
      >
        <span className="chat-thinking__icon" aria-hidden="true">
          💭
        </span>
        <span className="chat-thinking__label">
          {streaming ? 'Réflexion en cours…' : 'Réflexion'}
        </span>
        <ChevronIcon />
      </button>

      {expanded && <pre className="chat-thinking__content">{thinking}</pre>}
    </div>
  );
}

/** Chevron du bloc repliable, pivoté à 90° quand il est déplié (via aria-expanded). */
function ChevronIcon() {
  return (
    <svg
      className="chat-thinking__chevron"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="m9 6 6 6-6 6" />
    </svg>
  );
}