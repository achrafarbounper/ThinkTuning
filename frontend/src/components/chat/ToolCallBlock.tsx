import { useState } from 'react';
import type { ToolCallData } from './types';

interface ToolCallBlockProps {
  /** Appels d'outils observés pendant le tour de l'agent, dans l'ordre. */
  calls: ToolCallData[];
}

/**
 * Timeline des appels d'outils du mode Agent, façon GitHub Copilot :
 * une ligne compacte par outil (« 🔧 web_search → ✔ · 320 ms »), dépliable
 * pour révéler les arguments et l'aperçu du résultat.
 *
 * Rendu uniquement pour les messages de l'assistant ayant au moins un appel,
 * AU-DESSUS de la bulle de réponse (comme le bloc « Réflexion »).
 */
export function ToolCallBlock({ calls }: ToolCallBlockProps) {
  const [expandedIndex, setExpandedIndex] = useState<number | null>(null);

  if (calls.length === 0) return null;

  return (
    <div className="chat-tools" role="list" aria-label="Appels d'outils de l'agent">
      {calls.map((call, index) => {
        const expanded = expandedIndex === index;
        return (
          <div
            key={`${call.tool}-${index}`}
            className="chat-tool"
            data-status={call.status}
            role="listitem"
          >
            <button
              type="button"
              className="chat-tool__toggle"
              onClick={() => setExpandedIndex(expanded ? null : index)}
              aria-expanded={expanded}
              aria-label={`Détail de l'appel ${call.tool}`}
            >
              <span className="chat-tool__icon" aria-hidden="true">
                🔧
              </span>
              <code className="chat-tool__name">{call.tool}</code>
              <span className="chat-tool__status" aria-label={`Statut : ${call.status}`}>
                {call.status === 'running'
                  ? '⏳ en cours…'
                  : call.status === 'error'
                    ? '⚠ erreur'
                    : '✔'}
              </span>
              {typeof call.durationMs === 'number' && (
                <span className="chat-tool__duration">{formatDuration(call.durationMs)}</span>
              )}
            </button>

            {expanded && (
              <div className="chat-tool__detail">
                {call.args && (
                  <>
                    <p className="chat-tool__section">Arguments</p>
                    <pre className="chat-tool__pre">{call.args}</pre>
                  </>
                )}
                {call.summary && (
                  <>
                    <p className="chat-tool__section">Résultat</p>
                    <pre className="chat-tool__pre">{call.summary}</pre>
                  </>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** Affichage compact d'une durée en millisecondes (ex : « 320 ms », « 1,2 s »). */
function formatDuration(durationMs: number): string {
  if (durationMs >= 1000) return `${(durationMs / 1000).toFixed(1)} s`;
  return `${Math.round(durationMs)} ms`;
}
