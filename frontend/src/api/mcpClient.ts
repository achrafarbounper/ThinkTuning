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
 *   - `X-API-Key` : transmise quand configurée (l'endpoint MCP ne l'exige pas
 *     encore — durcissement auth prévu en S4/S5) ; sans effet si absente.
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
 */
export function parseSseData(body: string): string {
  const dataLines: string[] = [];
  for (const rawLine of body.split(/\r?\n/)) {
    if (rawLine.startsWith('data:')) {
      dataLines.push(rawLine.startsWith('data: ') ? rawLine.slice(6) : rawLine.slice(5));
    }
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
  actions?: OrchestrateMcpAction[];
  rounds_used?: number;
  tool_calls_used?: number;
  awaiting_approval?: boolean;
  request_id?: string;
  approval?: { tool?: string; reason?: string; args?: unknown };
  [key: string]: unknown;
}

/** Argumentaire d'un appel `tools/call orchestrate` depuis le dashboard. */
export interface OrchestrateMcpArgs {
  prompt: string;
  session_id?: string;
  scope?: string;
  enable_thinking?: boolean;
}

export interface OrchestrateMcpStreamEvent {
  thinking_delta?: string;
  delta?: string;
  tool?: Record<string, unknown>;
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
  const result = await client.callTool('orchestrate', { ...args });
  const text = result.content?.find((block) => block.type === 'text')?.text ?? '';
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
  const client = new McpSseClient(config);
  const streamArgs = { ...args, stream: true };
  const response = await client.streamTool('orchestrate', streamArgs);
  if (!response.body) {
    throw new McpTransportError('Le transport MCP n’a retourné aucun flux.', response.status);
  }
  let finalRpc: JsonRpcResponse | undefined;

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

    if (event.event === 'orchestrate.thinking') {
      onEvent({
        thinking_delta:
          typeof payload.thinking_delta === 'string' ? payload.thinking_delta : '',
      });
    } else if (event.event === 'orchestrate.tool') {
      onEvent({ tool: payload });
    } else if (
      event.event === 'orchestrate.done' ||
      event.event === 'orchestrate.error' ||
      event.event === 'message'
    ) {
      finalRpc = payload as unknown as JsonRpcResponse;
      onEvent({ rpc: finalRpc });
    }
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
    throw new McpTransportError(`L'agent MCP a échoué : ${text || 'erreur inconnue'}`, response.status);
  }
  try {
    return JSON.parse(text) as OrchestrateMcpResult;
  } catch {
    throw new McpTransportError(
      `Réponse d'orchestration non JSON : ${text.slice(0, 200)}`,
      response.status,
    );
  }
}
