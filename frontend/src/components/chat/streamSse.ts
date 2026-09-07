/**
 * Lecture d'un flux HTTP au format Server-Sent Events (SSE).
 *
 * Implémentation conforme à la spec « event-stream » :
 *   - un événement est un BLOC de lignes terminé par une ligne vide ;
 *   - plusieurs lignes `data:` d'un même bloc sont concaténées avec « \n » ;
 *   - une seule espace de tête est retirée après le champ (`data: x` → « x »),
 *     un `.trim()` détruirait des espaces significatifs en fin de payload ;
 *   - le champ `event:` nomme le bloc courant (défaut : « message »).
 *
 * Exemple de flux :
 *   data: {"delta": "Bon"}
 *   data: {"delta": "jour"}
 *
 *   data: [DONE]
 */

/** Découpe la valeur d'un champ SSE : retire UNE espace de tête éventuelle. */
function fieldValue(raw: string): string {
  return raw.startsWith(" ") ? raw.slice(1) : raw;
}

export async function* readSseEvents(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<string, void, unknown> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let dataLines: string[] = [];

  const dispatch = function* (): Generator<string, void, unknown> {
    if (dataLines.length) {
      yield dataLines.join("\n");
      dataLines = [];
    }
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      let newlineIndex: number;
      while ((newlineIndex = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, newlineIndex).replace(/\r$/, "");
        buffer = buffer.slice(newlineIndex + 1);

        if (line === "") {
          // Fin de bloc : on émet l'événement accumulé.
          yield* dispatch();
        } else if (line.startsWith("data:")) {
          dataLines.push(fieldValue(line.slice("data:".length)));
        }
        // Commentaires (`:…`) et autres champs (id:, retry:) ignorés.
      }
    }
    // Flux tronqué sans ligne vide finale : émet le dernier bloc.
    yield* dispatch();
  } finally {
    reader.releaseLock();
  }
}

/** Une trame SSE nominative : nom de l'événement + charge utile JSON texte. */
export interface NamedSseEvent {
  /** Nom de l'événement (« agent.plan »...). « message » si le champ est absent. */
  event: string;
  /** Charge utile brute (JSON à parser par l'appelant). */
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
  let buffer = "";
  let pendingEvent = "message";
  let dataLines: string[] = [];

  const dispatch = function* (): Generator<NamedSseEvent, void, unknown> {
    if (dataLines.length) {
      yield { event: pendingEvent, data: dataLines.join("\n") };
      dataLines = [];
      pendingEvent = "message";
    }
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      let newlineIndex: number;
      while ((newlineIndex = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, newlineIndex).replace(/\r$/, "");
        buffer = buffer.slice(newlineIndex + 1);

        if (line === "") {
          // Fin de bloc : l'éventuel `event:` du bloc est consommé ici.
          yield* dispatch();
        } else if (line.startsWith("event:")) {
          pendingEvent = fieldValue(line.slice("event:".length)) || "message";
        } else if (line.startsWith("data:")) {
          dataLines.push(fieldValue(line.slice("data:".length)));
        }
      }
    }
    // Flux tronqué sans ligne vide finale : émet le dernier bloc.
    yield* dispatch();
  } finally {
    reader.releaseLock();
  }
}
