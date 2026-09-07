import { useEffect, useRef, useState } from 'react';
import type { UIEvent } from 'react';

interface ThinkingBlockProps {
  /** Trace de raisonnement accumulée (peut être vide au démarrage). */
  thinking: string;
  /** Vrai tant que la réflexion est encore en cours de diffusion. */
  streaming: boolean;
}

/**
 * Distance (px) entre le bas du contenu et le bas de la boîte au-delà de
 * laquelle un défilement manuel de l'utilisateur interrompt le suivi
 * automatique. Même heuristique que SCROLL_THRESHOLD_PX de ChatWindow
 * (fenêtre principale), calibrée pour la hauteur réduite du bloc (240px).
 */
const FOLLOW_THRESHOLD_PX = 40;

/**
 * Bloc repliable affichant la trace de raisonnement de l'assistant,
 * façon DeepSeek R1 : déplié automatiquement pendant la génération puis
 * refermé à la fin (réouverture manuelle possible à tout moment).
 *
 * Pendant la diffusion, le contenu suit automatiquement le bas du bloc
 * (effet « flux » temps réel : le dernier fragment de réflexion reste
 * visible) — sauf si l'utilisateur remonte manuellement dans le contenu,
 * auquel cas il reprend la main jusqu'à son retour au bas (même heuristique
 * que le stick-to-bottom de la fenêtre de chat principale).
 *
 * Rendu uniquement pour les messages de l'assistant, AU-DESSUS de la bulle
 * de réponse ; disparaît si aucune réflexion n'a été produite.
 */
export function ThinkingBlock({ thinking, streaming }: ThinkingBlockProps) {
  const [expanded, setExpanded] = useState(streaming);

  // Référence du <pre> (contenu défilant) et drapeau de suivi du bas.
  const contentRef = useRef<HTMLPreElement>(null);
  const followRef = useRef(true);

  // Dépliage pendant la diffusion de la réflexion, refermeture à la fin.
  // Ajustement d'état PENDANT le rendu (pattern React documenté, cf. Settings)
  // plutôt qu'un useEffect + setState qui déclenche des rendus en cascade.
  const [prevStreaming, setPrevStreaming] = useState(streaming);
  if (streaming !== prevStreaming) {
    setPrevStreaming(streaming);
    setExpanded(streaming);
    // Nouvelle diffusion (ou fin) : le suivi repart du bas.
    followRef.current = true;
  }

  // Réouverture manuelle : le suivi du bas est réactivé. Pendant la
  // diffusion le bloc rejoint alors le dernier fragment affiché ; hors
  // diffusion, le <pre> fraîchement monté repart naturellement du début
  // (aucun forçage de scroll sur une trace terminée).
  const [prevExpanded, setPrevExpanded] = useState(expanded);
  if (expanded !== prevExpanded) {
    setPrevExpanded(expanded);
    followRef.current = true;
  }

  // Suivi automatique : à chaque fragment diffusé, on colle le bas du bloc
  // tant que l'utilisateur n'a pas coupé le suivi (onScroll ci-dessous).
  useEffect(() => {
    if (!streaming || !expanded || !followRef.current) return;
    const element = contentRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [thinking, expanded, streaming]);

  /** Défilement manuel détecté : coupure du suivi si l'utilisateur remonte. */
  const handleContentScroll = (event: UIEvent<HTMLPreElement>) => {
    const element = event.currentTarget;
    const distanceFromBottom =
      element.scrollHeight - element.scrollTop - element.clientHeight;
    followRef.current = distanceFromBottom <= FOLLOW_THRESHOLD_PX;
  };

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

      {expanded && (
        <pre
          ref={contentRef}
          className="chat-thinking__content"
          onScroll={handleContentScroll}
        >
          {thinking}
        </pre>
      )}
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