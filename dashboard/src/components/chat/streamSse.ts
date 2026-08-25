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
