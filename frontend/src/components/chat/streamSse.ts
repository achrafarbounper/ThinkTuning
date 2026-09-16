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

/**
 * Options de lecture SSE communes.
 *
 * `onActivity` est invoqué à CHAQUE bloc réseau lu (`reader.read()`), y compris
 * les chunks ne contenant que des commentaires de garde (`: heartbeat` du
 * serveur MCP). Il sert à alimenter un timeout d'inactivité (L3 — SCRUM-154) :
 * tant que le serveur émet des heartbeats, le flux est vivant et le client ne
 * doit PAS interrompre la lecture — seul un silence prolongé est une erreur.
 */
export interface SseReadOptions {
  /** Callback d'activité (chunk réseau brut reçu, contenu non décodé). */
  onActivity?: () => void;
  /**
   * Signal d'interruption (L3 — SCRUM-154).
   *
   * Ponte l'abort du transport vers la lecture pendante : sans lui, un
   * `AbortController.abort()` déclenché APRÈS la réception des en-têtes ne
   * touche PAS le corps de la réponse (le listener de transfert de
   * `streamTool` est retiré dès que le fetch résout) et la boucle resterait
   * bloquée pour toujours sur `reader.read()` — proxy mort = chat gelé.
   */
  signal?: AbortSignal;
}

export async function* readSseEvents(
  body: ReadableStream<Uint8Array>,
  options: SseReadOptions = {},
): AsyncGenerator<string, void, unknown> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let dataLines: string[] = [];

  // Course lecture / interruption : l'abort rejette la lecture pendante, ce
  // qui fait sortir la boucle (et propage l'AbortError à l'appelant, qui
  // distingue annulation utilisateur vs timeout d'inactivité).
  const signal = options.signal;
  let rejectOnAbort: ((reason: unknown) => void) | undefined;
  const abortPromise: Promise<never> | undefined = signal
    ? new Promise<never>((_, reject) => {
        rejectOnAbort = () => reject(new DOMException("Aborted", "AbortError"));
        if (signal.aborted) rejectOnAbort(new DOMException("Aborted", "AbortError"));
        else signal.addEventListener("abort", rejectOnAbort, { once: true });
      })
    : undefined;

  const dispatch = function* (): Generator<string, void, unknown> {
    if (dataLines.length) {
      yield dataLines.join("\n");
      dataLines = [];
    }
  };

  try {
    for (;;) {
      const pendingRead = reader.read();
      // Le perdant d'une course ne doit jamais devenir un rejet non géré.
      pendingRead.catch(() => undefined);
      const { done, value } = await (abortPromise
        ? Promise.race([pendingRead, abortPromise])
        : pendingRead);
      if (done) break;
      options.onActivity?.();

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
    if (rejectOnAbort) signal?.removeEventListener("abort", rejectOnAbort);
    // Annule le reader en best-effort (révoque les lectures pendantes) puis
    // relâche le verrou : le transport peut réutiliser/fermer le flux.
    // Garde défensif : une exception ici masquerait l'erreur d'origine.
    if (typeof reader.cancel === "function") void reader.cancel().catch(() => undefined);
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
  options: SseReadOptions = {},
): AsyncGenerator<NamedSseEvent, void, unknown> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let pendingEvent = "message";
  let dataLines: string[] = [];

  // Course lecture / interruption (même mécanique que readSseEvents).
  const signal = options.signal;
  let rejectOnAbort: ((reason: unknown) => void) | undefined;
  const abortPromise: Promise<never> | undefined = signal
    ? new Promise<never>((_, reject) => {
        rejectOnAbort = () => reject(new DOMException("Aborted", "AbortError"));
        if (signal.aborted) rejectOnAbort(new DOMException("Aborted", "AbortError"));
        else signal.addEventListener("abort", rejectOnAbort, { once: true });
      })
    : undefined;

  const dispatch = function* (): Generator<NamedSseEvent, void, unknown> {
    if (dataLines.length) {
      yield { event: pendingEvent, data: dataLines.join("\n") };
      dataLines = [];
      pendingEvent = "message";
    }
  };

  try {
    for (;;) {
      const pendingRead = reader.read();
      // Le perdant d'une course ne doit jamais devenir un rejet non géré.
      pendingRead.catch(() => undefined);
      const { done, value } = await (abortPromise
        ? Promise.race([pendingRead, abortPromise])
        : pendingRead);
      if (done) break;
      options.onActivity?.();

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
    if (rejectOnAbort) signal?.removeEventListener("abort", rejectOnAbort);
    // Annule le reader en best-effort (révoque les lectures pendantes) puis
    // relâche le verrou : le transport peut réutiliser/fermer le flux.
    // Garde défensif : une exception ici masquerait l'erreur d'origine.
    if (typeof reader.cancel === "function") void reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
