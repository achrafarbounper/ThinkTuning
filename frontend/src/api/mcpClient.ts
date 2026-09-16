/**
 * mcpClient.ts
 * ---------------------------------------------------------------------
 * Transport MCP-over-SSE du dashboard (S7 — v3.0.0 MCP-First, tâche 20
 * de docs/mcp/IMPLEMENTATION_PLAN.md).
 *
 * Le serveur MCP (`app/infrastructure/mcp/mcp_server_sse.py`) expose
 * `POST /mcp/sse` : le corps de la requête est un message JSON-RPC 2.0 et la
 * réponse est un flux `text/event-stream` à événement unique (`event: message`
 * portant la réponse JSON-RPC — style « streamable HTTP » du protocole MCP
 * 2025-06-18, sans dépendance à un SDK MCP).
 *
 * Ce module est la couture de la migration du dashboard : quand le mode « MCP »
 * du chat est actif, les tours d'assistant partent vers le tool `orchestrate`
 * (S6, tâche 16) au lieu de l'API HTTP legacy (`/api/v1/agent/ask/core`).
 * Transport autonome (aucune dépendance à clientCore), testable hors React
 * (fetch mocké — voir mcpClient.test.ts).
 *
 * Conventions MCP respectées :
 *   - `Mcp-Session-Id` : identifiant de session généré côté client (le serveur
 *     l'écho ; CORS n'expose pas cet en-tête au JS, on garde le nôtre) ;
 *   - `X-Client-Id` : identifiant du dashboard pour l'audit MCP
 *     (`agent_audit.subject` — cf. docs/mcp/MCP_SECURITY.md) ;
 *   - `X-API-Key` : transmise quand configurée (le transport MCP l'EXIGE
 *     depuis P5 — `MCP_AUTH_REQUIRED`, fail-closed ; même clé que la surface
 *     REST). Sans clé configurée, l'appel répond 401 avec l'enveloppe v1.
 */

import { readNamedSseEvents } from '../components/chat/streamSse';

// Chemin du transport MCP (NON versionné : surface MCP à discovery propre).
export const MCP_SSE_PATH = '/mcp/sse';

/** Identifiant du dashboard dans l'audit MCP (agent_audit.subject). */
export const MCP_CLIENT_ID = 'thinktuning-dashboard';

/** Délai par défaut d'un aller-retour MCP (un tour orchestrate peut être long). */
export const MCP_DEFAULT_TIMEOUT_MS = 120_000;

/** Version du protocole MCP négociée par le serveur (handshake initialize). */
export const MCP_PROTOCOL_VERSION = '2025-06-18';

/** Base URL par défaut (même convention que clientCore.DEFAULT_BASE_URL). */
const DEFAULT_MCP_BASE_URL: string =
  import.meta.env.VITE_API_URL ?? 'http://localhost:8000';

/** Erreur de transport/protocole MCP (statut HTTP + code JSON-RPC le cas échéant). */
export class McpTransportError extends Error {
  /** Statut HTTP de l'échec (0 : réseau indisponible ou timeout). */
  status: number;
  /** Code d'erreur JSON-RPC (-32700 parse, -32601 method, -32000 serveur…). */
  code?: number;

  constructor(message: string, status = 0, code?: number) {
    super(message);
    this.name = 'McpTransportError';
    this.status = status;
    this.code = code;
  }
}

export interface McpClientConfig {
  /** Base URL de l'API (défaut : VITE_API_URL / http://localhost:8000). */
  baseUrl?: string;
  /** Clé API (en-tête X-API-Key), optionnelle côté MCP. */
  apiKey?: string;
  /** Identifiant client (X-Client-Id) pour l'audit — défaut : dashboard. */
  clientId?: string;
  /** Identifiant de session MCP (Mcp-Session-Id) — défaut : généré. */
  sessionId?: string;
  /** Timeout d'un aller-retour (ms) — défaut : MCP_DEFAULT_TIMEOUT_MS. */
  timeoutMs?: number;
  /** Signal d'annulation externe (ex : bouton Stop du chat). */
  signal?: AbortSignal;
}

/** Bloc de contenu MCP d'une réponse tools/call. */
export interface McpContentBlock {
  type: string;
  text?: string;
}

/** Résultat tools/call (contrat MCP : content + isError). */
export interface McpToolCallResult {
  content: McpContentBlock[];
  isError: boolean;
  [key: string]: unknown;
}

/** Un tool exposé par le serveur (tools/list, filtré par scope serveur). */
export interface McpToolInfo {
  name: string;
  description?: string;
  inputSchema?: Record<string, unknown>;
  annotations?: Record<string, unknown>;
  [key: string]: unknown;
}

/** Résultat du handshake initialize. */
export interface McpInitializeResult {
  protocolVersion: string;
  capabilities: Record<string, unknown>;
  serverInfo: { name: string; version?: string } & Record<string, unknown>;
  [key: string]: unknown;
}

/** Réponse JSON-RPC 2.0 décodée depuis le flux SSE. */
interface JsonRpcResponse<T = unknown> {
  jsonrpc: '2.0';
  id: number | string | null;
  result?: T;
  error?: { code: number; message: string; data?: unknown };
}

/**
 * Extrait la charge utile `data:` du flux SSE à événement unique renvoyé par
 * `POST /mcp/sse`. Tolérant : lignes de garde (`: ping`) des notifications,
 * champs `event:`/`id:` ignorés, plusieurs lignes `data:` concaténées avec
 * « \n » (spécification SSE — même convention que streamSse.ts).
 *
 * Sentinelle de terminaison MCP : une ligne `data: [DONE]` clôt le flux —
 * elle est ignorée (et interrompt la lecture) au lieu d'être concaténée au
 * JSON, ce qui rendait `JSON.parse` impossible pour initialize / ping /
 * tools/list (P0 — SCRUM-151).
 */
export function parseSseData(body: string): string {
  const dataLines: string[] = [];
  for (const rawLine of body.split(/\r?\n/)) {
    if (!rawLine.startsWith('data:')) continue;
    const value = rawLine.startsWith('data: ') ? rawLine.slice(6) : rawLine.slice(5);
    if (value.trim() === '[DONE]') break;
    dataLines.push(value);
  }
  if (dataLines.length === 0) {
    throw new McpTransportError(
      `Réponse MCP sans charge utile SSE : ${body.slice(0, 200)}`,
      200,
    );
  }
  return dataLines.join('\n');
}

/** Client MCP-over-SSE minimal (initialize / ping / tools / resources). */
export class McpSseClient {
  private readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly clientId: string;
  private readonly sessionId?: string;
  private readonly timeoutMs: number;
  private readonly signal?: AbortSignal;
  private nextId = 1;

  constructor(config: McpClientConfig = {}) {
    this.baseUrl = (config.baseUrl ?? DEFAULT_MCP_BASE_URL).replace(/\/+$/, '');
    this.apiKey = config.apiKey ?? '';
    this.clientId = config.clientId || MCP_CLIENT_ID;
    this.sessionId = config.sessionId;
    this.timeoutMs = config.timeoutMs ?? MCP_DEFAULT_TIMEOUT_MS;
    this.signal = config.signal;
  }

  /**
   * Aller-retour JSON-RPC : POST du message, lecture du flux SSE, décodage de
   * la réponse. Lève `McpTransportError` (HTTP, JSON-RPC, timeout, payload
   * invalide) avec un message lisible côté utilisateur.
   */
  async call<T>(method: string, params?: Record<string, unknown>): Promise<T> {
    const id = this.nextId++;
    const url = `${this.baseUrl}${MCP_SSE_PATH}`;

    const controller = new AbortController();
    // Annulation externe (bouton Stop du chat) fusionnée avec le timeout.
    const externalSignal = this.signal;
    const onExternalAbort = () => controller.abort();
    if (externalSignal) {
      if (externalSignal.aborted) controller.abort();
      else externalSignal.addEventListener('abort', onExternalAbort, { once: true });
    }
    const timer = window.setTimeout(() => controller.abort(), this.timeoutMs);

    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: this.headers(),
        body: JSON.stringify({
          jsonrpc: '2.0',
          id,
          method,
          ...(params !== undefined ? { params } : {}),
        }),
        signal: controller.signal,
      });

      if (!response.ok) {
        throw await mcpHttpError(response);
      }

      const payload = parseSseData(await response.text());
      let reply: JsonRpcResponse<T>;
      try {
        reply = JSON.parse(payload) as JsonRpcResponse<T>;
      } catch {
        throw new McpTransportError(
          `Réponse MCP non JSON : ${payload.slice(0, 200)}`,
          response.status,
        );
      }
      if (reply.error) {
        throw new McpTransportError(
          `MCP ${method} a échoué : ${reply.error.message}`,
          response.status,
          reply.error.code,
        );
      }
      return reply.result as T;
    } catch (error) {
      if (error instanceof McpTransportError) throw error;
      if (controller.signal.aborted) {
        throw new McpTransportError(
          `La requête MCP (${method}) a dépassé le délai autorisé.`,
          0,
        );
      }
      throw new McpTransportError(
        `Impossible de joindre le serveur MCP à ${url} (${describeNetworkError(error)}).`,
        0,
      );
    } finally {
      externalSignal?.removeEventListener('abort', onExternalAbort);
      window.clearTimeout(timer);
    }
  }

  /** Handshake MCP : version de protocole + capacités du serveur. */
  initialize(): Promise<McpInitializeResult> {
    return this.call<McpInitializeResult>('initialize', {
      protocolVersion: MCP_PROTOCOL_VERSION,
      clientInfo: { name: MCP_CLIENT_ID, version: '1.0.0' },
    });
  }

  /** Sonnette de disponibilité (result attendu : `{}`). */
  ping(): Promise<Record<string, never>> {
    return this.call<Record<string, never>>('ping');
  }

  /** Catalogue des tools VISIBLES par ce client (filtrage scope côté serveur). */
  async listTools(): Promise<McpToolInfo[]> {
    const result = await this.call<{ tools: McpToolInfo[] }>('tools/list');
    return result.tools ?? [];
  }

  /** Appelle un tool MCP (validation scope/policy côté serveur). */
  async callTool(
    name: string,
    args: Record<string, unknown> = {},
  ): Promise<McpToolCallResult> {
    return this.call<McpToolCallResult>('tools/call', { name, arguments: args });
  }

  /** Ouvre un appel tool en conservant le corps SSE lisible par le caller. */
  async streamTool(
    name: string,
    args: Record<string, unknown> = {},
  ): Promise<Response> {
    const id = this.nextId++;
    const controller = new AbortController();
    const externalSignal = this.signal;
    const onExternalAbort = () => controller.abort();
    if (externalSignal) {
      if (externalSignal.aborted) controller.abort();
      else externalSignal.addEventListener('abort', onExternalAbort, { once: true });
    }
    const timer = window.setTimeout(() => controller.abort(), this.timeoutMs);
    const url = `${this.baseUrl}${MCP_SSE_PATH}`;
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: this.headers(),
        body: JSON.stringify({
          jsonrpc: '2.0',
          id,
          method: 'tools/call',
          params: { name, arguments: args },
        }),
        signal: controller.signal,
      });
      if (!response.ok) throw await mcpHttpError(response);
      if (!response.body) {
        throw new McpTransportError('Le transport MCP n’a retourné aucun flux.', response.status);
      }
      return response;
    } catch (error) {
      if (error instanceof McpTransportError) throw error;
      if (controller.signal.aborted) {
        throw new McpTransportError(`La requête MCP (${name}) a dépassé le délai autorisé.`, 0);
      }
      throw new McpTransportError(
        `Impossible de joindre le serveur MCP à ${url} (${describeNetworkError(error)}).`,
        0,
      );
    } finally {
      externalSignal?.removeEventListener('abort', onExternalAbort);
      window.clearTimeout(timer);
    }
  }

  /** En-têtes du transport : JSON-RPC + SSE + identité client/session MCP. */
  private headers(): Record<string, string> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      'X-Client-Id': this.clientId,
      'Mcp-Session-Id': this.sessionId ?? createMcpSessionId(),
    };
    if (this.apiKey) headers['X-API-Key'] = this.apiKey;
    return headers;
  }
}

// --- Helpers de transport -----------------------------------------------------

/**
 * Identifiant de session MCP (en-tête `Mcp-Session-Id`). Généré une fois par
 * instance de client (bien que `POST /mcp/sse` soit sans état, l'en-tête reste
 * transmis pour la traçabilité / le futur mode streamable HTTP stateful).
 */
let sharedMcpSessionId = '';
function createMcpSessionId(): string {
  if (sharedMcpSessionId) return sharedMcpSessionId;
  sharedMcpSessionId =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `mcp-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  return sharedMcpSessionId;
}

/** Normalise une erreur HTTP non-OK en McpTransportError (message lisible). */
async function mcpHttpError(response: Response): Promise<McpTransportError> {
  const generic = `Le serveur MCP a répondu ${response.status} (${response.statusText})`;
  try {
    const body = await response.text();
    // Réponse JSON-RPC d'erreur transportée via SSE (endpoint HTTP stateless)…
    try {
      const parsed = JSON.parse(body) as { error?: { message?: string } };
      if (parsed.error?.message) {
        return new McpTransportError(parsed.error.message, response.status);
      }
    } catch {
      /* corps non JSON : on retombe sur l'enveloppe v1 */
    }
    // …ou enveloppe v1 FastAPI ({ detail: "…" }).
    try {
      const parsed = JSON.parse(body) as { detail?: unknown };
      const detail =
        typeof parsed.detail === 'string'
          ? parsed.detail
          : Array.isArray(parsed.detail)
            ? JSON.stringify(parsed.detail)
            : '';
      if (detail) return new McpTransportError(detail, response.status);
    } catch {
      /* idem */
    }
    return new McpTransportError(`${generic}${body ? ` — ${body.slice(0, 200)}` : ''}`, response.status);
  } catch {
    return new McpTransportError(generic, response.status);
  }
}

/** Décrit une erreur réseau (fetch rejetée) en une phrase lisible. */
function describeNetworkError(error: unknown): string {
  if (error instanceof TypeError) return error.message;
  return error instanceof Error ? error.message : String(error);
}

// --- Intégration dashboard (données.read *readonly* dans le flux du tool) -----

/** Une action d'un run orchestrate (trace — lecture seule dans le dashboard). */
export interface OrchestrateMcpAction {
  tool: string;
  status?: string;
  summary?: string;
  [key: string]: unknown;
}

/**
 * Contrat de sortie du tool MCP `orchestrate` (S6, tâche 16) : le handler
 * sérialise `AgentRunResult` en JSON texte (cf. orchestrate_tool.py).
 */
export interface OrchestrateMcpResult {
  answer: string;
  thinking?: string;
  status: string;
  phase?: string;
  plan?: Array<Record<string, unknown>>;
  workers?: Array<Record<string, unknown>>;
  actions?: OrchestrateMcpAction[];
  rounds_used?: number;
  tool_calls_used?: number;
  awaiting_approval?: boolean;
  /** DEMANDE d'approbation (POST /api/agent/approvals/{id}/…). */
  request_id?: string;
  /** Décision structurée du gate (outil, args, motif). */
  approval?: { tool?: string; reason?: string; args?: unknown };
  /** Identifiant DURABLE du run — DISTINCT de request_id (P0 SCRUM-151). */
  run_id?: string;
  /** Sous-tâche (worker) en attente — reprise ciblée. */
  task_id?: string;
  orchestration?: {
    mode?: string;
    event?: string;
    fallback?: string;
    reason?: string;
    source?: string;
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

/** Argumentaire d'un appel `tools/call orchestrate` depuis le dashboard. */
export interface OrchestrateMcpArgs {
  prompt: string;
  session_id?: string;
  scope?: string;
  enable_thinking?: boolean;
  mode?: 'mono_agent' | 'multi_agent';
  model?: string;
  parallel?: boolean;
  event_granularity?: 'minimal' | 'summary' | 'verbose';
  /** Identifiant de la DEMANDE D'APPROBATION approuvée à rejouer. */
  resume_request_id?: string;
  /** Identifiant DURABLE du run à reprendre (distinct de resume_request_id). */
  run_id?: string;
  /** Sous-tâche (worker) à reprendre ciblée (reprise déclarative). */
  task_id?: string;
}

/** Curseur durable exposé par `orchestrate.started` (L1 SCRUM-152). */
export interface McpOrchestrateStarted {
  /** Identifiant DURABLE du run (null : store durable indisponible). */
  run_id?: string | null;
  /** Vraie reprise d'un run déjà engagé (vs run simplement préparé). */
  resumed?: boolean;
  /** Dernière séquence d'événement déjà persistée (curseur de replay). */
  last_sequence?: number;
}

/** Événement durable rejoué par `orchestrate_events` (L1 SCRUM-152). */
export interface OrchestrateReplayEvent {
  event: string;
  sequence?: number;
  phase?: string;
  worker_id?: string | null;
  [key: string]: unknown;
}

export interface OrchestrateMcpStreamEvent {
  thinking_delta?: string;
  delta?: string;
  tool?: Record<string, unknown>;
  multi_agent?: Record<string, unknown>;
  orchestration?: Record<string, unknown>;
  /** Événement métier de phase (synthèse, deadline globale, etc.). */
  phase?: Record<string, unknown>;
  /** Prélude `orchestrate.started` : curseur de reprise mémorisable. */
  started?: McpOrchestrateStarted;
  rpc?: JsonRpcResponse;
}

/**
 * Tour d'assistant via MCP : `tools/call orchestrate` (transport POST /mcp/sse).
 *
 * Point d'entrée du mode « MCP » du chat (tâche 20) : l'agentic run part vers
 * MCP au lieu de l'API HTTP legacy (`/api/v1/agent/ask/core` — read-only en
 * mode MCP_FIRST). Le résultat est le bloc `content[0].text` (JSON) décodé ;
 * `isError: true` (ou texte non JSON) lève une erreur lisible.
 */
export async function orchestrateViaMcp(
  args: OrchestrateMcpArgs,
  config?: McpClientConfig,
): Promise<OrchestrateMcpResult> {
  const client = new McpSseClient(config);
  const result = await client.callTool('orchestrate', {
    mode: 'multi_agent',
    ...args,
  });
  const text = result.content?.find((block: McpContentBlock) => block.type === 'text')?.text ?? '';
  if (result.isError) {
    throw new McpTransportError(
      `L'agent MCP a échoué : ${text || 'erreur inconnue (isError: true)'}`,
      200,
    );
  }

  try {
    return JSON.parse(text) as OrchestrateMcpResult;
  } catch {
    // Le handler renvoie toujours du JSON ; texte inattendu => verdict explicite.
    throw new McpTransportError(
      `Réponse d'orchestration non JSON : ${text.slice(0, 200)}`,
      200,
    );
  }
}

/**
 * Variante progressive de `orchestrateViaMcp`.
 *
 * Le serveur récent émet des événements `orchestrate.*`; un serveur ancien
 * peut encore répondre par l'unique événement MCP `message`, qui est décodé
 * comme fallback sans perdre la compatibilité.
 */
export async function orchestrateViaMcpStream(
  args: OrchestrateMcpArgs,
  onEvent: (event: OrchestrateMcpStreamEvent) => void,
  config?: McpClientConfig,
): Promise<OrchestrateMcpResult> {
  // `streamTool` can only observe response headers. Keep a second controller
  // for the body so a stalled proxy/LLM cannot leave the chat busy forever.
  const streamController = new AbortController();
  const externalSignal = config?.signal;
  const abortFromCaller = () => streamController.abort();
  if (externalSignal) {
    if (externalSignal.aborted) streamController.abort();
    else externalSignal.addEventListener('abort', abortFromCaller, { once: true });
  }
  const timeout = window.setTimeout(
    () => streamController.abort(),
    config?.timeoutMs ?? MCP_DEFAULT_TIMEOUT_MS,
  );
  const streamArgs = { mode: 'multi_agent' as const, ...args, stream: true };
  let response: Response;
  try {
    response = await new McpSseClient({ ...config, signal: streamController.signal }).streamTool(
      'orchestrate',
      streamArgs,
    );
    if (!response.body) {
      throw new McpTransportError('Le transport MCP n’a retourné aucun flux.', response.status);
    }
  } catch (error) {
    externalSignal?.removeEventListener('abort', abortFromCaller);
    window.clearTimeout(timeout);
    throw error;
  }
  let finalRpc: JsonRpcResponse | undefined;
  let legacyFinalAnswer: OrchestrateMcpResult | undefined;
  // P1 : dernier message d'échec observé (agent.error, orchestrate.error,
  // phase en échec) — utilisé pour un message d'erreur explicite au lieu
  // d'une bulle vide ou d'un « Réponse non JSON » trompeur.
  let lastFailureMessage: string | undefined;
  // Type du dernier événement d'échec (pour distinguer orchestrate.error).
  let lastFailureEvent: string | undefined;

  try {
    for await (const event of readNamedSseEvents(response.body)) {
      if (event.data === '[DONE]') break;
      let payload: Record<string, unknown>;
      try {
        payload = JSON.parse(event.data) as Record<string, unknown>;
      } catch {
        throw new McpTransportError(
          `Événement MCP non JSON : ${event.data.slice(0, 200)}`,
          response.status,
        );
      }

      // L1 (SCRUM-152) : ``orchestrate.started`` expose le curseur de reprise
      // (run_id + last_sequence) — le chat peut le mémoriser puis rejouer les
      // événements manquants après une coupure via ``replayOrchestrateEvents``.
      if (event.event === 'orchestrate.started') {
        const started: McpOrchestrateStarted = {
          run_id: typeof payload.run_id === 'string' && payload.run_id ? payload.run_id : null,
          resumed: payload.resumed === true,
          last_sequence:
            typeof payload.last_sequence === 'number' && Number.isFinite(payload.last_sequence)
              ? payload.last_sequence
              : 0,
        };
        onEvent({ started });
      }

      if (event.event === 'orchestrate.thinking') {
        onEvent({
          thinking_delta:
            typeof payload.thinking_delta === 'string' ? payload.thinking_delta : '',
        });
      } else if (event.event === 'orchestrate.tool') {
        const coreTool =
          payload.core_tool && typeof payload.core_tool === 'object'
            ? (payload.core_tool as Record<string, unknown>)
            : payload;
        onEvent({ tool: coreTool });
      } else if (
        event.event === 'orchestrate.start' ||
        event.event === 'orchestrate.started' ||
        event.event === 'orchestrate.lead' ||
        event.event === 'orchestrate.worker' ||
        event.event === 'orchestrate.synthesis' ||
        event.event === 'orchestrate.synthesizing' ||
        event.event === 'agent.plan' ||
        event.event === 'agent.resuming' ||
        event.event === 'agent.worker.start' ||
        event.event === 'agent.worker.tool' ||
        event.event === 'agent.worker.result' ||
        event.event === 'agent.worker.error' ||
        event.event === 'agent.worker.approval' ||
        event.event === 'agent.worker.thinking' ||
        event.event === 'agent.synthesizing' ||
        event.event === 'agent.phase' ||
        event.event === 'agent.done' ||
        event.event === 'agent.error' ||
        event.event === 'checkpoint_recovered' ||
        event.event === 'orchestration_fallback'
      ) {
        onEvent({ multi_agent: payload });
        if (event.event === 'agent.phase') {
          onEvent({ phase: payload });
          // P1 : mémorise les phases en échec (synthesis_timeout, deadline…)
          // pour enrichir le message d'erreur final.
          const status = typeof payload.status === 'string' ? payload.status : '';
          if (status === 'timeout' || status === 'error' || status === 'failed') {
            lastFailureEvent = 'agent.phase';
            lastFailureMessage =
              typeof payload.reason === 'string' && payload.reason
                ? payload.reason === 'synthesis_timeout'
                  ? 'La synthèse a dépassé son délai ; les résultats partiels sont conservés.'
                  : payload.reason === 'orchestration_deadline_reached'
                    ? 'La durée maximale de l’orchestration a été atteinte ; les résultats partiels sont conservés.'
                    : `La phase ${String(payload.phase ?? 'inconnue')} a échoué (${payload.reason}).`
                : 'Une phase de l’orchestration a échoué.';
          }
        }
        if (event.event === 'orchestration_fallback') {
          onEvent({ orchestration: payload });
        }
        // P1 : mémorise les erreurs agent même sans agent.done ultérieur.
        if (event.event === 'agent.error') {
          lastFailureEvent = 'agent.error';
          if (typeof payload.message === 'string' && payload.message.trim()) {
            lastFailureMessage = payload.message;
          } else if (typeof payload.summary === 'string' && payload.summary.trim()) {
            lastFailureMessage = payload.summary;
          }
        }
        if (event.event === 'agent.done') {
          const answer =
            typeof payload.answer === 'string'
              ? payload.answer
              : typeof payload.final_answer === 'string'
                ? payload.final_answer
                : '';
          if (answer) {
            legacyFinalAnswer = {
              answer,
              status: typeof payload.status === 'string' ? payload.status : 'completed',
            };
          }
        }
      } else if (
        event.event === 'orchestrate.done' ||
        event.event === 'orchestrate.error' ||
        event.event === 'message'
      ) {
        finalRpc = payload as unknown as JsonRpcResponse;
        onEvent({ rpc: finalRpc });
        // P1 : `orchestrate.error` porte le texte d'échec dans result.content
        // (isError: true) — on l'extrait pour un message explicite.
        if (event.event === 'orchestrate.error') {
          lastFailureEvent = 'orchestrate.error';
          try {
            const result = (finalRpc as JsonRpcResponse<McpToolCallResult>).result;
            const blockText =
              result && typeof result === 'object'
                ? result.content?.find((block) => block.type === 'text')?.text
                : undefined;
            if (typeof blockText === 'string' && blockText.trim()) {
              // Le serveur peut émettre une erreur synthétique JSON
              // (reason: orchestration_stream_interrupted) quand le flux se
              // termine sans événement final : message lisible plutôt que JSON brut.
              try {
                const inner = JSON.parse(blockText) as {
                  reason?: string;
                  failure_phase?: string;
                };
                if (inner && inner.reason === 'orchestration_stream_interrupted') {
                  lastFailureMessage =
                    'Le flux MCP s’est interrompu avant la synthèse (réponse finale manquante) ; ' +
                    'les événements agent.worker.result / agent.phase déjà affichés sont conservés.';
                } else {
                  lastFailureMessage = blockText.slice(0, 500);
                }
              } catch {
                lastFailureMessage = blockText.slice(0, 500);
              }
            }
          } catch {
            /* conservation du message précédent */
          }
        }
      }

      // Let the chat paint each reasoning/tool frame before the next buffered
      // network frame (and especially before the final JSON-RPC response).
      if (
        event.event === 'orchestrate.thinking' ||
        event.event === 'orchestrate.tool' ||
        event.event === 'orchestrate.worker' ||
        event.event === 'orchestrate.synthesis'
      ) {
        await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
      }
    }
  } catch (error) {
    if (streamController.signal.aborted) {
      throw new McpTransportError(
        externalSignal?.aborted
          ? 'La requête MCP a été annulée.'
          : 'La réponse MCP a dépassé le délai autorisé.',
        0,
      );
    }
    throw error;
  } finally {
    externalSignal?.removeEventListener('abort', abortFromCaller);
    window.clearTimeout(timeout);
  }

  const text =
    finalRpc?.result && typeof finalRpc.result === 'object'
      ? ((finalRpc.result as McpToolCallResult).content?.find((block) => block.type === 'text')?.text ?? '')
      : '';
  if (finalRpc?.error) {
    throw new McpTransportError(
      `MCP orchestrate a échoué : ${finalRpc.error.message}`,
      response.status,
      finalRpc.error.code,
    );
  }
  if (finalRpc?.result && (finalRpc.result as McpToolCallResult).isError) {
    // P1 : message d'échec observé en cours de flux plutôt que le texte brut.
    throw new McpTransportError(
      `L'agent MCP a échoué : ${lastFailureMessage || text || 'erreur inconnue'}`,
      response.status,
    );
  }
  if (!text && legacyFinalAnswer) {
    return legacyFinalAnswer;
  }
  // P1 : le serveur a fermé sans JSON-RPC final mais avec un agent.done
  // porteur de réponse (cas nominal des tests de repli) — déjà retourné
  // ci-dessus. Ici, sans texte ET sans réponse finale, on signale l'échec
  // observé (agent.error / phase) plutôt qu'une bulle vide.
  if (!text && !legacyFinalAnswer && lastFailureEvent) {
    throw new McpTransportError(
      `L'agent MCP a échoué (${lastFailureEvent}) : ${lastFailureMessage || 'erreur inconnue'}`,
      response.status,
    );
  }
  let parsed: OrchestrateMcpResult;
  try {
    parsed = JSON.parse(text) as OrchestrateMcpResult;
  } catch {
    if (legacyFinalAnswer) return legacyFinalAnswer;
    throw new McpTransportError(
      `Réponse d'orchestration non JSON : ${text.slice(0, 200)}`,
      response.status,
    );
  }
  if (!parsed.answer && legacyFinalAnswer) {
    return { ...parsed, answer: legacyFinalAnswer.answer };
  }
  if (
    parsed.status !== 'awaiting_approval' &&
    (typeof parsed.answer !== 'string' || !parsed.answer.trim())
  ) {
    throw new McpTransportError(
      "L'orchestration MCP s'est terminée sans réponse finale.",
      response.status,
    );
  }
  return parsed;
}

export interface ReplayOrchestrateEventsResult {
  run_id: string;
  /** NOUVEAU curseur à mémoriser pour un replay incrémental suivant. */
  last_sequence: number;
  events: OrchestrateReplayEvent[];
}

/**
 * Rejoue les événements durables d'un run MCP après une coupure (L1 — SCRUM-152).
 *
 * Consomme le tool `orchestrate_events` (transport SSE) : le serveur émet
 * `replay_started`, une suite d'`orchestrate.replay` (payloads durables
 * portant leur `sequence`), `replay_completed` (curseur final) puis [DONE].
 * Le callback reçoit chaque événement dans l'ordre du run ; le résultat
 * expose le NOUVEAU curseur (`last_sequence`) à mémoriser — un replay
 * ultérieur repart de ce curseur sans rejouer l'historique complet.
 */
export async function replayOrchestrateEvents(
  runId: string,
  afterSequence: number,
  onEvent: (event: OrchestrateReplayEvent) => void,
  config?: McpClientConfig,
): Promise<ReplayOrchestrateEventsResult> {
  const trimmedRunId = runId.trim();
  if (!trimmedRunId) {
    throw new McpTransportError('Le replay MCP exige un run_id de run durable.', 0);
  }
  const after = Math.max(0, Math.floor(afterSequence));
  const response = await new McpSseClient(config).streamTool('orchestrate_events', {
    run_id: trimmedRunId,
    after_sequence: after,
    replay: true,
    stream: true,
  });
  if (!response.body) {
    throw new McpTransportError('Le transport MCP n’a retourné aucun flux.', response.status);
  }
  const events: OrchestrateReplayEvent[] = [];
  let lastSequence = after;
  for await (const event of readNamedSseEvents(response.body)) {
    if (event.data === '[DONE]') break;
    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(event.data) as Record<string, unknown>;
    } catch {
      throw new McpTransportError(
        `Événement MCP non JSON : ${event.data.slice(0, 200)}`,
        response.status,
      );
    }
    if (event.event === 'orchestrate.replay') {
      const sequence =
        typeof payload.sequence === 'number' && Number.isFinite(payload.sequence)
          ? payload.sequence
          : undefined;
      if (sequence !== undefined && sequence > lastSequence) lastSequence = sequence;
      const replayed: OrchestrateReplayEvent = {
        ...payload,
        event: typeof payload.event === 'string' ? payload.event : event.event,
        ...(sequence !== undefined ? { sequence } : {}),
      };
      events.push(replayed);
      onEvent(replayed);
    } else if (event.event === 'replay_completed') {
      if (
        typeof payload.last_sequence === 'number' &&
        Number.isFinite(payload.last_sequence) &&
        payload.last_sequence > lastSequence
      ) {
        lastSequence = payload.last_sequence;
      }
    } else if (event.event === 'replay.error') {
      throw new McpTransportError(
        `Le replay MCP a échoué : ${String(payload.error ?? 'erreur inconnue')}`,
        response.status,
      );
    }
  }
  return { run_id: trimmedRunId, last_sequence: lastSequence, events };
}
