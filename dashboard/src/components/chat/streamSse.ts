/**
 * Lecture d'un flux HTTP au format Server-Sent Events (SSE).
 */

/**
 * Consomme le corps d'une réponse SSE et produit successivement la charge
 * utile (`data: …`) de chaque événement, ligne par ligne.
 *
 * Exemple de flux :
 *   data: {"delta": "Bon"}
 *   data: {"delta": "jour"}
 *   data: [DONE]
 */
export async function* readSseEvents(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<string, void, unknown> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;

      buffer += decoder.decode(value, { stream: true });

      // Les événements SSE sont délimités par des sauts de ligne.
      let newlineIndex: number;
      while ((newlineIndex = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, newlineIndex).replace(/\r$/, '');
        buffer = buffer.slice(newlineIndex + 1);

        if (line.startsWith('data:')) {
          yield line.slice('data:'.length).trim();
        }
        // Les lignes vides / commentaires / autres champs sont ignorés.
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/** Une trame SSE nominative : nom de l'événement + charge utile JSON texte. */
export interface NamedSseEvent {
  /** Nom de l'événement (« agent.plan »...). « message » si le champ est absent. */
  event: string;
  /** Charge utile brute de la ligne `data:` (JSON à parser par l'appelant). */
  data: string;
}

/**
 * Variante de {@link readSseEvents} pour les flux utilisant le champ `event:`
 * du protocole SSE (ex : POST /api/agent/multi/ask/stream qui émet des
 * événements nommés agent.plan, agent.worker.start, agent.done...).
 *
 * La sentinelle de fin de flux reste `data: [DONE]` : elle est restituée
 * telle quelle (data === '[DONE]') afin que l'appelant puisse s'arrêter.
 */
export async function* readNamedSseEvents(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<NamedSseEvent, void, unknown> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let pendingEvent = 'message';

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;

      buffer += decoder.decode(value, { stream: true });

      let newlineIndex: number;
      while ((newlineIndex = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, newlineIndex).replace(/\r$/, '');
        buffer = buffer.slice(newlineIndex + 1);

        if (line.startsWith('event:')) {
          // Nom de l'événement du bloc courant (arrive avant sa ligne data:).
          pendingEvent = line.slice('event:'.length).trim() || 'message';
          continue;
        }
        if (line.startsWith('data:')) {
          yield { event: pendingEvent, data: line.slice('data:'.length).trim() };
          pendingEvent = 'message';
        }
        // Les lignes vides / commentaires / autres champs sont ignorés.
      }
    }
  } finally {
    reader.releaseLock();
  }
}
