/**
 * useAssistantTurns.ts — L3 (SCRUM-154)
 * ---------------------------------------------------------------------
 * Moteur des TOURS de conversation, extrait de ChatWindow (découpage en
 * hooks cohérents « tours » / « approbations »).
 *
 * Route un tour utilisateur vers l'un des quatre canaux (mutuellement
 * exclusifs) :
 *  - MCP (S7)     : POST /mcp/sse, tool `orchestrate` — union discriminée
 *    `McpOrchestrateEvent` + trace partagée `useMultiAgentTrace` ;
 *  - Multi-agents : POST /api/v1/agent/multi/ask/stream (SSE agent.*) ;
 *  - Agent (v2)   : noyau POST /api/v1/agent/ask/core[/stream] ;
 *  - Chat (défaut): POST /api/v1/chat/ai (SSE delta).
 *
 * Détient aussi les mutations de messages (deltas coalescés, timeline
 * d'outils, plan/workers) et l'interruption (AbortController).
 */

import { useCallback, useRef } from 'react';
import type { Dispatch, MutableRefObject, SetStateAction } from 'react';
import { readNamedSseEvents, readSseEvents } from './streamSse';
import type {
  AgentAskResponse,
  ChatMessageData,
  ChatRequestBody,
  ChatStreamEvent,
  MultiAgentPlanTask,
  MultiAgentStreamEvent,
  MultiAgentWorkerState,
  PendingApprovalData,
  ToolCallData,
  ToolCallStatus,
} from './types';
import {
  AI_ENDPOINT,
  apiErrorMessage,
  CORE_ASK_ENDPOINT,
  CORE_ASK_STREAM_ENDPOINT,
  createId,
  MULTI_ASK_STREAM_ENDPOINT,
  MULTI_SSE_MODE,
  nowIso,
  resolveApiKey,
  resolveAuthHeaders,
  resolveBaseUrl,
} from './chatTransport';
import {
  makeMcpErrorActionable,
  McpTransportError,
  orchestrateViaMcpStream,
} from '../../api/mcpClient';
import type { McpOrchestrateEvent } from '../../api/mcpClient';

/** Dépendances du moteur de tours (injectées par ChatWindow). */
export interface AssistantTurnsDeps {
  /** Clé API des Settings (mémoire de session) — prioritaire pour MCP. */
  sessionApiKey: string;
  /** Identifiant de la conversation active ('' = création à la volée). */
  sessionId: string;
  /** Modèle LLM choisi ('' = défaut serveur). */
  selectedModel: string;
  /** Mode « Réflexion » (thinking_delta affiché). */
  enableThinking: boolean;
  /** Mode MCP (transport /mcp/sse). */
  mcpMode: boolean;
  /** Mode Multi-agents (SSE agent.*). */
  multiMode: boolean;
  /** Mode Agent v2 (noyau core). */
  coreMode: boolean;
  /** Sous-mode d'orchestration MCP. */
  mcpAgentMode: 'mono_agent' | 'multi_agent';
  /** Miroir du flag « occupé » (lecture dans les callbacks asynchrones). */
  isLoadingRef: MutableRefObject<boolean>;
  /** Setter du flag « occupé » (état React). */
  setIsLoading: Dispatch<SetStateAction<boolean>>;
  /** Miroir + état des messages. */
  messagesRef: MutableRefObject<ChatMessageData[]>;
  setMessages: Dispatch<SetStateAction<ChatMessageData[]>>;
  /** Contrôleur d'interruption courant (bouton Stop). */
  abortRef: MutableRefObject<AbortController | null>;
  /** Enregistre une demande d'approbation (carte HITL). */
  setPendingApproval: (approval: PendingApprovalData) => void;
  /** Événement MCP -> trace partagée (useMultiAgentTrace). */
  onTraceEvent: (event: McpOrchestrateEvent) => void;
  /** Nouveau tour : réinitialise la trace partagée. */
  onTraceReset: () => void;
}

/** Résultat exposé par le moteur de tours. */
export interface AssistantTurnsApi {
  sendMessage: (text: string) => Promise<void>;
  askMcpTurn: (
    assistantId: string,
    prompt: string,
    controller: AbortController,
    resumeRequestId?: string,
    runId?: string,
    taskId?: string,
  ) => Promise<void>;
  askMultiAgentTurn: (
    assistantId: string,
    prompt: string,
    controller: AbortController,
    resumeRequestId?: string,
    taskId?: string,
  ) => Promise<void>;
  askCoreTurn: (
    assistantId: string,
    prompt: string,
    controller: AbortController,
    resumeRequestId?: string,
  ) => Promise<void>;
  stopGeneration: () => void;
  /** Décharge immédiatement les fragments SSE coalescés (clôture de bulle). */
  flushStreamBuffer: () => void;
  /** Patch ciblé d'un message (exposé pour le miroir de trace de ChatWindow). */
  patchMessage: (id: string, patch: Partial<ChatMessageData>) => void;
  /** Identifiant de la bulle assistant en cours (miroir de trace). */
  activeAssistantIdRef: MutableRefObject<string>;
}

export function useAssistantTurns(deps: AssistantTurnsDeps): AssistantTurnsApi {
  const {
    sessionApiKey,
    sessionId,
    selectedModel,
    enableThinking,
    mcpMode,
    multiMode,
    coreMode,
    mcpAgentMode,
    isLoadingRef,
    messagesRef,
    setMessages,
    setIsLoading,
    abortRef,
    setPendingApproval,
    onTraceEvent,
    onTraceReset,
  } = deps;
  return useTurnsApi({
    sessionApiKey,
    sessionId,
    selectedModel,
    enableThinking,
    mcpMode,
    multiMode,
    coreMode,
    mcpAgentMode,
    isLoadingRef,
    messagesRef,
    setMessages,
    setIsLoading,
    abortRef,
    setPendingApproval,
    onTraceEvent,
    onTraceReset,
  });
}

/**
 * Construit l'API de tours. Séparé du hook pour garder la fonction hook
 * triviale (les callbacks n'ont pas besoin de se recréer à chaque rendu :
 * les valeurs volatiles — sessionId, modèle, modes — sont relues via le
 * sac de dépendances capturé à la création).
 */
function useTurnsApi(deps: AssistantTurnsDeps): AssistantTurnsApi {
  const {
    sessionApiKey,
    sessionId,
    selectedModel,
    enableThinking,
    mcpMode,
    multiMode,
    coreMode,
    mcpAgentMode,
    isLoadingRef,
    messagesRef,
    setMessages,
    setIsLoading,
    abortRef,
    setPendingApproval,
    onTraceEvent,
    onTraceReset,
  } = deps;

  /** Identifiant de la bulle assistant en cours de streaming (miroir trace). */
  const activeAssistantIdRef = useRef('');

  /**
   * Coalescing des fragments SSE (perf) : chaque événement réseau n'entraîne
   * plus un setState immédiat — les deltas (réponse ET réflexion) sont
   * accumulés dans un tampon par message, déchargé au plus UNE fois par
   * image (requestAnimationFrame). Un provider local rapide (LM Studio)
   * peut émettre > 60 événements/s : sans coalescing, chaque token déclenche
   * un rendu React complet de la bulle active (SCRUM-101).
   */
  const streamBufferRef = useRef(new Map<string, { content: string; thinking: string }>());
  const flushScheduledRef = useRef(false);

  /** Décharge le tampon de fragments dans l'état (un seul setMessages). */
  const flushStreamBuffer = useCallback(() => {
    flushScheduledRef.current = false;
    if (streamBufferRef.current.size === 0) return;
    const pending = streamBufferRef.current;
    streamBufferRef.current = new Map();
    setMessages((previous) =>
      previous.map((message) => {
        const patch = pending.get(message.id);
        if (!patch) return message;
        return {
          ...message,
          content: patch.content ? message.content + patch.content : message.content,
          thinking: patch.thinking
            ? (message.thinking ?? '') + patch.thinking
            : message.thinking,
          thinkingStreaming: patch.thinking ? true : message.thinkingStreaming,
        };
      }),
    );
  }, [setMessages]);

  /** Programme le déchargement : au plus une exécution par image. */
  const scheduleFlush = useCallback(() => {
    if (flushScheduledRef.current) return;
    flushScheduledRef.current = true;
    // rAF indisponible (environnement sans boucle de rendu, ex. jsdom) : repli.
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(flushStreamBuffer);
    } else {
      setTimeout(flushStreamBuffer, 16);
    }
  }, [flushStreamBuffer]);

  /** Ajoute un fragment de texte au message en cours de streaming. */
  const appendDelta = useCallback(
    (id: string, delta: string) => {
      const entry = streamBufferRef.current.get(id) ?? { content: '', thinking: '' };
      entry.content += delta;
      streamBufferRef.current.set(id, entry);
      scheduleFlush();
    },
    [scheduleFlush],
  );

  /** Ajoute un fragment de réflexion au message en cours de streaming. */
  const appendThinkingDelta = useCallback(
    (id: string, delta: string) => {
      if (!delta) return;
      // La réflexion est une surface UX temps réel : elle ne doit pas attendre
      // le buffer rAF du texte final, sinon elle n'apparaît qu'à la fin du run.
      setMessages((previous) =>
        previous.map((message) =>
          message.id === id
            ? {
                ...message,
                thinking: (message.thinking ?? '') + delta,
                thinkingStreaming: true,
              }
            : message,
        ),
      );
    },
    [setMessages],
  );

  /** Modifie certains champs d'un message (fin de streaming, erreur…). */
  const patchMessage = useCallback(
    (id: string, patch: Partial<ChatMessageData>) => {
      setMessages((previous) =>
        previous.map((message) => (message.id === id ? { ...message, ...patch } : message)),
      );
    },
    [setMessages],
  );

  /** Ajoute un appel d'outil « running » à la timeline du message (tool_start). */
  const appendToolCall = useCallback(
    (id: string, call: ToolCallData) => {
      setMessages((previous) =>
        previous.map((message) =>
          message.id === id
            ? { ...message, toolCalls: [...(message.toolCalls ?? []), call] }
            : message,
        ),
      );
    },
    [setMessages],
  );

  /** Clôture le dernier appel « running » portant le même outil (tool_result). */
  const completeToolCall = useCallback(
    (id: string, result: NonNullable<ChatStreamEvent['tool_result']>) => {
      setMessages((previous) =>
        previous.map((message) => {
          if (message.id !== id || !message.toolCalls?.length) return message;
          const calls = [...message.toolCalls];
          for (let index = calls.length - 1; index >= 0; index -= 1) {
            if (calls[index].status === 'running' && calls[index].tool === result.tool) {
              calls[index] = {
                ...calls[index],
                status: ((result.status as ToolCallStatus) || 'ok') satisfies ToolCallStatus,
                summary: result.summary,
                durationMs: result.duration_ms,
              };
              break;
            }
          }
          return { ...message, toolCalls: calls };
        }),
      );
    },
    [setMessages],
  );

  /** Enregistre le plan validé par le superviseur (agent.plan). */
  const setMultiPlan = useCallback(
    (id: string, plan: NonNullable<ChatMessageData['multiPlan']>) => {
      patchMessage(id, { multiPlan: plan });
    },
    [patchMessage],
  );

  /** Démarre un worker dans la trace (agent.worker.start). */
  const startMultiWorker = useCallback(
    (id: string, worker: MultiAgentWorkerState) => {
      setMessages((previous) =>
        previous.map((message) =>
          message.id === id
            ? { ...message, multiWorkers: [...(message.multiWorkers ?? []), worker] }
            : message,
        ),
      );
    },
    [setMessages],
  );

  /** Clôture un worker par task_id (agent.worker.result / agent.worker.error). */
  const completeMultiWorker = useCallback(
    (
      id: string,
      task_id: string,
      patch: Partial<MultiAgentWorkerState>,
      finalStatus: MultiAgentWorkerState['status'],
    ) => {
      setMessages((previous) =>
        previous.map((message) => {
          if (message.id !== id || !message.multiWorkers?.length) return message;
          const workers = [...message.multiWorkers];
          for (let index = workers.length - 1; index >= 0; index -= 1) {
            if (workers[index].task_id === task_id && workers[index].status === 'running') {
              workers[index] = {
                ...workers[index],
                ...patch,
                status: finalStatus,
                durationMs: patch.durationMs ?? workers[index].durationMs,
              };
              break;
            }
          }
          return { ...message, multiWorkers: workers };
        }),
      );
    },
    [setMessages],
  );

  /** Interrompt proprement la génération en cours. */
  const stopGeneration = useCallback(() => {
    abortRef.current?.abort();
  }, [abortRef]);

  /**
   * Tour de chat via la surface MCP (S7 — tâche 20) : POST /mcp/sse puis
   * `tools/call orchestrate`. L3 (SCRUM-154) : le callback consomme l'union
   * discriminée `McpOrchestrateEvent` (un `switch` sur `event.kind` — plus de
   * champs optionnels à deviner) et alimente en parallèle la trace partagée
   * (`useMultiAgentTrace`) via `onTraceEvent`.
   *
   * Erreurs : `McpTransportError` est rendue ACTIONNABLE (401/403/503/réseau,
   * cf. `makeMcpErrorActionable`) ; la trace accumulée est conservée (jamais
   * effacée silencieusement) et un repli HTTP legacy est proposé via
   * `fallbackPrompt` (bouton « Renvoyer via HTTP » dans la bulle).
   */
  const askMcpTurn = useCallback(
    async (
      assistantId: string,
      prompt: string,
      controller: AbortController,
      resumeRequestId?: string,
      runId?: string,
      taskId?: string,
    ): Promise<void> => {
      activeAssistantIdRef.current = assistantId;
      let streamedFinalAnswer = false;
      // Résumés des workers (agent.worker.result) : repli P1 si la réponse
      // finale est vide — la bulle n'est JAMAIS laissée vide sans explication.
      const workerSummaries: string[] = [];

      try {
        const result = await orchestrateViaMcpStream(
          {
            prompt,
            session_id: sessionId || undefined,
            enable_thinking: enableThinking,
            mode: mcpAgentMode,
            model: selectedModel || undefined,
            parallel: mcpAgentMode === 'multi_agent',
            event_granularity: 'summary',
            // P0 (SCRUM-151) — reprise ciblée : resume_request_id (demande
            // d'approbation APPROUVÉE) + run_id (run durable) + task_id
            // (sous-tâche bloquée). Les trois sont INDÉPENDANTS.
            ...(resumeRequestId ? { resume_request_id: resumeRequestId } : {}),
            ...(runId ? { run_id: runId } : {}),
            ...(taskId ? { task_id: taskId } : {}),
          },
          (event) => {
            onTraceEvent(event);
            switch (event.kind) {
              case 'started':
                // Prélude (run_id durable + curseur de replay) : la trace
                // l'absorbe (applyMcpEvent) ; rien à peindre dans la bulle.
                return;
              case 'thinking':
                appendThinkingDelta(assistantId, event.delta);
                return;
              case 'tool': {
                const toolName = typeof event.tool.tool === 'string' ? event.tool.tool : undefined;
                if (!toolName) return;
                if (event.tool.event === 'tool_start') {
                  appendToolCall(assistantId, {
                    tool: toolName,
                    args:
                      typeof event.tool.args === 'string'
                        ? event.tool.args
                        : event.tool.args && typeof event.tool.args === 'object'
                          ? JSON.stringify(event.tool.args)
                          : undefined,
                    status: 'running',
                  });
                } else {
                  completeToolCall(assistantId, {
                    tool: toolName,
                    status: event.tool.status === 'error' ? 'error' : 'ok',
                    summary:
                      typeof event.tool.result_summary === 'string'
                        ? event.tool.result_summary
                        : undefined,
                    duration_ms:
                      typeof event.tool.duration_ms === 'number'
                        ? event.tool.duration_ms
                        : undefined,
                  });
                }
                return;
              }
              case 'phase': {
                // Phase métier en échec (timeout / deadline) : les résultats
                // partiels sont conservés — notice, pas d'erreur bloquante.
                if (event.status === 'timeout' || event.status === 'error' || event.status === 'failed') {
                  const reason = event.reason ?? 'phase_failure';
                  const message =
                    reason === 'synthesis_timeout'
                      ? 'La synthèse a dépassé son délai ; les résultats partiels sont conservés.'
                      : reason === 'orchestration_deadline_reached'
                        ? 'La durée maximale de l’orchestration a été atteinte ; les résultats partiels sont conservés.'
                        : 'Une phase de l’orchestration a échoué ; les résultats disponibles sont conservés.';
                  patchMessage(assistantId, { orchestrationNotice: message });
                }
                return;
              }
              case 'multi_agent': {
                // Événement d'orchestration brut (agent.* / orchestrate.*) :
                // mêmes règles que le mode Multi-agents HTTP (plan, workers,
                // réflexion, réponse finale, erreur JAMAIS avalée).
                const multiEvent = event.payload;
                const eventName = event.event;

                // Mode « Réflexion » multi-agents (agent.worker.thinking).
                if (
                  eventName === 'agent.worker.thinking' &&
                  typeof multiEvent.thinking === 'string'
                ) {
                  appendThinkingDelta(assistantId, multiEvent.thinking);
                }

                const taskId = typeof multiEvent.task_id === 'string'
                  ? multiEvent.task_id
                  : typeof multiEvent.worker_id === 'string'
                    ? multiEvent.worker_id
                    : undefined;

                if (
                  eventName === 'orchestrate.start' ||
                  eventName === 'orchestrate.started' ||
                  eventName === 'orchestrate.lead' ||
                  eventName === 'agent.plan'
                ) {
                  const plan = multiEvent.plan;
                  if (Array.isArray(plan)) {
                    setMultiPlan(assistantId, plan as MultiAgentPlanTask[]);
                  }
                } else if (
                  (
                    eventName === 'orchestrate.worker' ||
                    eventName === 'agent.worker.start' ||
                    eventName === 'agent.worker.tool' ||
                    eventName === 'agent.worker.result' ||
                    eventName === 'agent.worker.error' ||
                    eventName === 'agent.worker.approval'
                  ) &&
                  taskId
                ) {
                  const status = String(multiEvent.status ?? 'running');
                  const workerStatus: MultiAgentWorkerState['status'] =
                    status === 'error' || status === 'failed'
                      ? 'error'
                      : status === 'ok' || status === 'completed'
                        ? 'ok'
                        : status === 'awaiting_approval'
                          ? 'awaiting_approval'
                          : 'running';
                  const existing = messagesRef.current
                    .find((message) => message.id === assistantId)
                    ?.multiWorkers?.some((worker) => worker.task_id === taskId);
                  // Mémorise le résumé dès le premier événement porteur (repli P1 :
                  // bulle jamais vide) — y compris quand le worker est créé ici.
                  const summaryText =
                    typeof multiEvent.summary === 'string' && multiEvent.summary.trim()
                      ? multiEvent.summary.trim()
                      : undefined;
                  if (!existing) {
                    if (summaryText) workerSummaries.push(summaryText);
                    startMultiWorker(assistantId, {
                      task_id: taskId,
                      role: String(multiEvent.role ?? multiEvent.worker_id ?? 'worker'),
                      subtask: typeof multiEvent.subtask === 'string' ? multiEvent.subtask : undefined,
                      status: workerStatus,
                    });
                    if (workerStatus !== 'running') {
                      completeMultiWorker(assistantId, taskId, {}, workerStatus);
                    }
                  } else if (workerStatus !== 'running') {
                    if (summaryText) workerSummaries.push(summaryText);
                    completeMultiWorker(
                      assistantId,
                      taskId,
                      {
                        summary: typeof multiEvent.summary === 'string' ? multiEvent.summary : undefined,
                        message: typeof multiEvent.message === 'string' ? multiEvent.message : undefined,
                        durationMs: typeof multiEvent.duration_ms === 'number' ? multiEvent.duration_ms : undefined,
                      },
                      workerStatus,
                    );
                  }
                }

                if (
                  (eventName === 'agent.done' ||
                    eventName === 'orchestrate.done' ||
                    eventName === 'orchestrate.synthesis') &&
                  typeof multiEvent.final_answer === 'string' &&
                  multiEvent.final_answer.trim()
                ) {
                  appendDelta(assistantId, multiEvent.final_answer);
                  streamedFinalAnswer = true;
                } else if (
                  eventName === 'agent.done' &&
                  typeof multiEvent.answer === 'string' &&
                  multiEvent.answer.trim()
                ) {
                  appendDelta(assistantId, multiEvent.answer);
                  streamedFinalAnswer = true;
                }
                if (eventName === 'agent.error' || eventName === 'orchestrate.error') {
                  // P0 : une erreur d'orchestration n'est JAMAIS avalée — elle
                  // est affichée dans la bulle (miroir du chemin non-MCP).
                  const message =
                    typeof multiEvent.message === 'string' && multiEvent.message.trim()
                      ? multiEvent.message
                      : 'Échec de l’orchestration MCP.';
                  // Ajoute tout contenu partiel d déjà collecté avant de lever :
                  // le catch ci-dessous l'affichera via patchMessage(error).
                  const partial =
                    workerSummaries.length > 0
                      ? `Résultats partiels des workers :\n${workerSummaries.join('\n')}\n\n`
                      : '';
                  throw new Error(`${partial}${message}`);
                }
                return;
              }
              case 'done': {
                // Réponse finale (agent.done / orchestrate.done au niveau flux).
                const answer =
                  typeof event.payload.answer === 'string'
                    ? event.payload.answer
                    : typeof event.payload.final_answer === 'string'
                      ? event.payload.final_answer
                      : undefined;
                if (answer && answer.trim()) {
                  appendDelta(assistantId, answer);
                  streamedFinalAnswer = true;
                }
                return;
              }
              case 'fallback': {
                // Repli orchestration (multi → mono) : notice visible, pas une
                // erreur — la trace et les workers déjà affichés sont conservés.
                const reason = event.reason ?? 'capacité multi-agent indisponible';
                patchMessage(assistantId, {
                  orchestrationNotice:
                    `Le mode multi-agent MCP a été remplacé par le mode mono-agent (${reason}).`,
                });
                return;
              }
              case 'error': {
                // agent.error / orchestrate.error au niveau flux : jamais avalé.
                const partial =
                  workerSummaries.length > 0
                    ? `Résultats partiels des workers :\n${workerSummaries.join('\n')}\n\n`
                    : '';
                throw new Error(`${partial}${event.message || 'Échec de l’orchestration MCP.'}`);
              }
              // intent / skipped / rpc : absorbés par la trace (applyMcpEvent) ;
              // rpc est la réponse JSON-RPC finale, traitée après le flux.
              default:
                return;
            }
          },
          {
            baseUrl: resolveBaseUrl(),
            // Transport MCP (P5) : X-API-Key EXIGÉE côté backend (fail-closed,
            // cf. mcp_server_sse.py) — le Bearer JWT n'y est pas accepté.
            // Source PRIORITAIRE : la clé saisie dans les Settings de la
            // session ; repli config persistée (legacy) / VITE_API_KEY.
            apiKey: sessionApiKey || resolveApiKey(),
            signal: controller.signal,
          },
        );

        // Run en attente d'une décision humaine (policy APPROVE) : la carte de
        // validation s'affiche — l'approbation passe par le canal HTTP
        // whitelisté (/api/v1/agent/approvals), non bloqué par MCP_FIRST ; la
        // relance réutilise resume_request_id ET run_id (P0 SCRUM-151).
        if (result.awaiting_approval && result.request_id) {
          setPendingApproval({
            requestId: result.request_id,
            prompt,
            tool: result.approval?.tool ?? 'outil inconnu',
            reason: result.approval?.reason ?? 'validation humaine requise',
            args: result.approval?.args as Record<string, unknown> | undefined,
            origin: 'mcp',
            runId: result.run_id,
            taskId: result.task_id,
          });
          return;
        }
        if (result.plan && Array.isArray(result.plan)) {
          setMultiPlan(assistantId, result.plan as unknown as MultiAgentPlanTask[]);
        }
        const fallback = result.orchestration;
        if (fallback?.event === 'orchestration_fallback' || fallback?.fallback) {
          const reason = fallback.reason ?? 'capacité multi-agent indisponible';
          patchMessage(assistantId, {
            orchestrationNotice:
              `Le mode multi-agent MCP a été remplacé par le mode mono-agent (${reason}).`,
          });
        }
        const resultReason =
          typeof result.orchestration?.reason === 'string'
            ? result.orchestration.reason
            : typeof result.phase === 'string'
              ? result.phase
              : undefined;
        if (result.status === 'partial_success' && resultReason) {
          const message =
            resultReason === 'synthesis_timeout'
              ? 'La synthèse a dépassé son délai ; les résultats partiels sont conservés.'
              : resultReason === 'orchestration_deadline_reached'
                ? 'La durée maximale de l’orchestration a été atteinte ; les résultats partiels sont conservés.'
                : 'Réponse partielle : certains résultats n’ont pas pu être finalisés.';
          patchMessage(assistantId, { orchestrationNotice: message });
        }
        // completed / rejected / error : le run porte la réponse finale.
        // P1 : si elle est vide, repli sur les résumés des workers, puis sur
        // un message explicite — la bulle n'est JAMAIS vide sans explication.
        flushStreamBuffer();
        if (!streamedFinalAnswer) {
          const finalText = (result.answer || '').trim();
          if (finalText) {
            appendDelta(assistantId, result.answer || '');
          } else if (workerSummaries.length > 0) {
            patchMessage(assistantId, {
              orchestrationNotice:
                'Réponse finale indisponible ; résultats partiels des workers affichés.',
            });
            appendDelta(
              assistantId,
              `Résultats partiels des workers :\n${workerSummaries.join('\n')}`,
            );
          } else {
            patchMessage(assistantId, {
              error:
                "L'orchestration MCP s'est terminée sans réponse finale. Relancez la demande ou vérifiez les journaux du serveur (thinktuning.mcp.sse).",
            });
          }
        }
        flushStreamBuffer();
        patchMessage(assistantId, { thinkingStreaming: false });
      } catch (error) {
        if (controller.signal.aborted) {
          // Annulation volontaire (bouton Stop) : ni erreur, ni repli — la
          // trace accumulée est conservée telle quelle.
          return;
        }
        if (error instanceof McpTransportError) {
          // L3 : message ACTIONNABLE (401 clé API, 403 scope, 503 MCP_FIRST,
          // réseau) + repli HTTP legacy proposé via fallbackPrompt. La trace
          // n'est PAS réinitialisée : ce qui a été affiché reste visible.
          const actionable = makeMcpErrorActionable(error, resolveBaseUrl());
          const fallbackViable = [0, 401, 403, 503].includes(error.status);
          patchMessage(assistantId, {
            error: actionable,
            ...(fallbackViable ? { fallbackPrompt: prompt } : {}),
          });
          return;
        }
        throw error; // erreurs d'orchestration (agent.error…) : catch de sendMessage
      } finally {
        flushStreamBuffer();
      }
    },
    [
      appendDelta,
      appendThinkingDelta,
      appendToolCall,
      completeToolCall,
      completeMultiWorker,
      enableThinking,
      flushStreamBuffer,
      mcpAgentMode,
      messagesRef,
      onTraceEvent,
      patchMessage,
      selectedModel,
      sessionApiKey,
      sessionId,
      setMultiPlan,
      setPendingApproval,
      startMultiWorker,
    ],
  );

  /**
   * Tour MULTI-AGENTS (superviseur / workers) : POST /api/v1/agent/multi/
   * ask/stream — entrée NORMALE du mode « Multi-agents » (trace temps réel :
   * plan + workers + réflexion) ET reprise native d'une sous-tâche bloquée
   * sur une validation humaine (resume_request_id).
   */
  const askMultiAgentTurn = useCallback(
    async (
      assistantId: string,
      prompt: string,
      controller: AbortController,
      resumeRequestId?: string,
      taskId?: string,
    ): Promise<void> => {
      // task_id n'est pas transmis à /multi/ask/stream : la reprise native est
      // ciblée côté orchestrateur via resume_request_id (empreinte SHA-256).
      void taskId;
      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        ...resolveAuthHeaders(),
      };

      // Contrat backend (schema MultiAskRequest) : champs snake_case.
      // - parallel: true → les sous-tâches INDÉPENDANTES sont parallélisées ;
      // - resume_request_id → REPRISE NATIVE : l'orchestrateur re-dispatche
      //   UNIQUEMENT le worker bloqué (action approuvée rejouée dans le même
      //   worker, empreinte SHA-256 revérifiée) puis re-synthétise.
      const body: Record<string, unknown> = {
        prompt,
        mode: MULTI_SSE_MODE,
        parallel: true,
      };
      if (selectedModel) body.model = selectedModel;
      if (resumeRequestId) body.resume_request_id = resumeRequestId;
      if (enableThinking) body.enable_thinking = true;

      const base = resolveBaseUrl();
      const response = await fetch(`${base}${MULTI_ASK_STREAM_ENDPOINT}`, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error(await apiErrorMessage(response));
      }

      const contentType = response.headers.get('content-type') ?? '';
      if (!(contentType.includes('text/event-stream') && response.body)) {
        // Repli JSON non streamé : le backend a répondu d'un bloc.
        const data = (await response.json()) as {
          status?: string;
          final_answer?: string;
          message?: string;
          plan?: NonNullable<ChatMessageData['multiPlan']>;
          pending_approvals?: Array<{
            task_id: string;
            role: string;
            request_id: string;
            approval?: { tool?: string; args?: Record<string, unknown>; reason?: string };
          }>;
        };
        if (data.plan) setMultiPlan(assistantId, data.plan);
        if (data.status === 'error') {
          throw new Error(data.message || 'Échec de l’orchestration multi-agents.');
        }
        const blockedTask = data.pending_approvals?.[0];
        if (data.status === 'awaiting_approval' && blockedTask?.request_id) {
          const subtask = data.plan?.find((t) => t.task_id === blockedTask.task_id)?.subtask;
          completeMultiWorker(
            assistantId,
            blockedTask.task_id,
            { message: data.final_answer ?? data.message },
            'awaiting_approval',
          );
          setPendingApproval({
            requestId: blockedTask.request_id,
            prompt: subtask ?? prompt,
            tool: blockedTask.approval?.tool ?? 'outil inconnu',
            reason: blockedTask.approval?.reason ?? 'validation humaine requise',
            args: blockedTask.approval?.args,
            origin: 'multi',
            taskId: blockedTask.task_id,
          });
          return;
        }
        appendDelta(assistantId, data.final_answer ?? data.message ?? '');
        return;
      }

      // Plan local : retrouve le texte d'une sous-tâche (reprise ciblée via
      // resume_request_id sur la sous-tâche bloquée).
      let planTasks: MultiAgentPlanTask[] = [];
      let finalAnswerReceived = false;
      const workerSummaries: string[] = [];
      for await (const frame of readNamedSseEvents(response.body)) {
        if (frame.data === '[DONE]') break;

        let event: MultiAgentStreamEvent;
        try {
          event = JSON.parse(frame.data) as MultiAgentStreamEvent;
        } catch {
          continue; // charge utile illisible : on ignore (tolérance)
        }

        switch (frame.event) {
          case 'agent.plan':
            if (event.plan?.length) {
              planTasks = event.plan;
              setMultiPlan(assistantId, event.plan);
            }
            break;
          case 'agent.worker.start':
            if (event.task_id) {
              startMultiWorker(assistantId, {
                task_id: event.task_id,
                role: event.role ?? '?',
                // agent.worker.start ne porte que task_id/role : la sous-tâche
                // affichée est retrouvée dans le plan reçu via agent.plan (le
                // code initial affichait plan[0] pour TOUS les workers).
                subtask: planTasks.find((task) => task.task_id === event.task_id)?.subtask,
                status: 'running',
              });
            }
            break;
          case 'agent.worker.result':
            if (event.task_id) {
              if (event.summary) workerSummaries.push(event.summary);
              completeMultiWorker(
                assistantId,
                event.task_id,
                { summary: event.summary, durationMs: event.duration_ms },
                'ok',
              );
            }
            break;
          case 'agent.worker.error':
            if (event.task_id) {
              completeMultiWorker(
                assistantId,
                event.task_id,
                { message: event.message, durationMs: event.duration_ms },
                'error',
              );
            }
            break;
          case 'agent.worker.approval':
            // Une sous-tâche attend une validation humaine : worker en
            // « awaiting_approval » (badge jaune) + carte Approuver/Refuser.
            // Prompt de reprise = texte de la SOUS-TÂCHE (reprise ciblée).
            if (event.task_id && event.request_id) {
              completeMultiWorker(
                assistantId,
                event.task_id,
                { message: event.message, durationMs: event.duration_ms },
                'awaiting_approval',
              );
              setPendingApproval({
                requestId: event.request_id,
                prompt:
                  planTasks.find((task) => task.task_id === event.task_id)?.subtask ?? prompt,
                tool: event.approval?.tool ?? 'outil inconnu',
                reason: event.approval?.reason ?? 'validation humaine requise',
                args: event.approval?.args,
                origin: 'multi',
                taskId: event.task_id,
              });
            }
            break;
          case 'agent.worker.thinking':
            if (event.thinking) appendThinkingDelta(assistantId, event.thinking);
            break;
          case 'agent.done': {
            // Orchestration interrompue sur une validation : le texte final
            // récapitulatif n'est affiché que si aucune carte n'est pendante.
            const finalAnswer = event.final_answer ?? event.answer;
            if (finalAnswer && !finalAnswer.startsWith('Validation humaine requise')) {
              appendDelta(assistantId, finalAnswer);
              finalAnswerReceived = true;
            }
            break;
          }
          case 'agent.error':
            throw new Error(event.message || 'Échec de l’orchestration multi-agents.');
          default:
            // agent.synthesizing et autres : rien à afficher pour l'instant.
            break;
        }
      }
      if (!finalAnswerReceived && workerSummaries.length > 0) {
        appendDelta(assistantId, workerSummaries.join('\n\n'));
      }
    },
    [
      appendDelta,
      appendThinkingDelta,
      completeMultiWorker,
      enableThinking,
      selectedModel,
      setMultiPlan,
      setPendingApproval,
      startMultiWorker,
    ],
  );

  /**
   * Tour de chat via le NOYAU agentique v2 : POST /api/v1/agent/ask/core/
   * stream (SSE : core_tool / delta / final), avec repli transparent sur le
   * POST bloquant /api/v1/agent/ask/core si le backend ne connaît pas le flux.
   * Même contrat AskResponse, y compris awaiting_approval → carte de
   * validation humaine (gate auto_approve / approve / reject).
   */
  const askCoreTurn = useCallback(
    async (
      assistantId: string,
      prompt: string,
      controller: AbortController,
      resumeRequestId?: string,
    ): Promise<void> => {
      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        ...resolveAuthHeaders(),
      };

      const base = resolveBaseUrl();
      /** Applique le statut final (contrat AskResponse du noyau). */
      const handleFinal = (data: AgentAskResponse, alreadyStreamed: boolean): void => {
        if (data.status === 'awaiting_approval' && data.request_id) {
          const tool = data.approval?.tool ?? 'outil inconnu';
          setPendingApproval({
            requestId: data.request_id,
            prompt,
            tool,
            reason: data.approval?.reason ?? 'validation humaine requise',
            args: data.approval?.args,
          });
          if (!alreadyStreamed) {
            appendDelta(assistantId, `[En attente de validation] L'action « ${tool} » nécessite votre décision avant exécution.`);
          }
          return;
        }
        // completed / rejected / error : la réponse backend porte l'explication.
        if (!alreadyStreamed) appendDelta(assistantId, data.response || '');
      };

      /** Repli : POST bloquant /api/v1/agent/ask/core (réponse d'un bloc). */
      const askCoreBlocking = async (): Promise<void> => {
        // Contrat backend (schéma AskRequest, repli bloquant) : champs en
        // snake_case, avec model/enable_thinking (parité AskStreamRequest).
        const blockingBody: {
          prompt: string;
          session_id?: string;
          resume_request_id?: string;
          model?: string;
          enable_thinking?: boolean;
        } = { prompt };
        if (sessionId) blockingBody.session_id = sessionId;
        if (resumeRequestId) blockingBody.resume_request_id = resumeRequestId;
        if (selectedModel) blockingBody.model = selectedModel;
        if (enableThinking) blockingBody.enable_thinking = true;

        const blockingResponse = await fetch(`${base}${CORE_ASK_ENDPOINT}`, {
          method: 'POST',
          headers,
          body: JSON.stringify(blockingBody),
          signal: controller.signal,
        });
        if (!blockingResponse.ok) {
          throw new Error(await apiErrorMessage(blockingResponse));
        }
        handleFinal((await blockingResponse.json()) as AgentAskResponse, false);
      };

      // Contrat backend (schéma AskStreamRequest) : champs en snake_case.
      const body: Record<string, unknown> = { prompt };
      if (sessionId) body.session_id = sessionId;
      if (resumeRequestId) body.resume_request_id = resumeRequestId;
      // Sélecteur de modèle de l'en-tête ('' = défaut serveur) : le schéma
      // AskStreamRequest expose `model` — sans ce champ, le choix serait
      // ignoré en mode Agent (le chat simple, lui, l'honore déjà).
      if (selectedModel) body.model = selectedModel;
      // Mode « Réflexion » : le noyau diffuse son raisonnement (thinking_delta).
      if (enableThinking) body.enable_thinking = true;

      const response = await fetch(`${base}${CORE_ASK_STREAM_ENDPOINT}`, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      // Backend sans endpoint de streaming : repli transparent.
      if (!response.ok && (response.status === 404 || response.status === 405)) {
        await askCoreBlocking();
        return;
      }
      if (!response.ok) {
        throw new Error(await apiErrorMessage(response));
      }

      const contentType = response.headers.get('content-type') ?? '';
      if (!(contentType.includes('text/event-stream') && response.body)) {
        // Réponse JSON classique (sans flux) : même rendu que le bloquant.
        handleFinal((await response.json()) as AgentAskResponse, false);
        return;
      }

      let streamedChars = 0;
      for await (const payload of readSseEvents(response.body)) {
        if (payload === '[DONE]') break;

        let event: ChatStreamEvent;
        try {
          event = JSON.parse(payload) as ChatStreamEvent;
        } catch {
          // Charge utile non JSON : affichée telle quelle (tolérance).
          appendDelta(assistantId, payload);
          streamedChars += payload.length;
          continue;
        }

        if (event.error) throw new Error(event.error);
        // Événements d'outils du noyau (frame « core_tool », même sémantique
        // que tool_start / tool_result du mode Agent) : affichés dans le chat.
        const toolFrame = event.core_tool;
        if (toolFrame?.tool) {
          if (toolFrame.event === 'tool_start') {
            appendToolCall(assistantId, {
              tool: toolFrame.tool,
              args: toolFrame.args == null ? undefined : JSON.stringify(toolFrame.args),
              status: 'running',
            });
          } else {
            completeToolCall(assistantId, {
              tool: toolFrame.tool,
              status: toolFrame.status === 'error' ? 'error' : 'ok',
              summary: toolFrame.summary,
              duration_ms: toolFrame.duration_ms,
            });
          }
        }
        if (event.delta) {
          appendDelta(assistantId, event.delta);
          streamedChars += event.delta.length;
        }
        // Mode « Réflexion » : trace de raisonnement du noyau (même contrat
        // thinking_delta que /api/v1/chat/ai).
        if (event.thinking_delta) appendThinkingDelta(assistantId, event.thinking_delta);
        if (event.final) handleFinal(event.final, streamedChars > 0);
      }
    },
    [
      appendDelta,
      appendThinkingDelta,
      appendToolCall,
      completeToolCall,
      enableThinking,
      selectedModel,
      sessionId,
      setPendingApproval,
    ],
  );

  /**
   * Envoie le message de l'utilisateur puis diffuse la réponse de l'IA.
   * Routeur : mode MCP > Multi-agents > Agent (v2) > Chat (défaut) — les
   * trois canaux d'agent restent mutuellement exclusifs (garantie faite par
   * les toggles de ChatWindow). Trace multi-agent attachée à la bulle à la
   * clôture du tour ; erreurs réseau : trace conservée (jamais effacée).
   */
  const sendMessage = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || isLoadingRef.current) return;

      // Historique exploitable par le backend (sans erreurs ni contenu vide).
      const history = messagesRef.current
        .filter((message) => !message.error && message.content.length > 0)
        .map((message) => ({ role: message.role, content: message.content }));

      const assistantId = createId();
      activeAssistantIdRef.current = assistantId;
      setMessages((previous) => [
        ...previous,
        { id: createId(), role: 'user', content: trimmed, createdAt: nowIso() },
        {
          id: assistantId,
          role: 'assistant',
          content: '',
          createdAt: nowIso(),
          streaming: true,
          thinking: '',
          thinkingStreaming: enableThinking,
        },
      ]);
      setIsLoading(true);

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        // Nouveau tour : la trace partagée repart de zéro (reset AVANT le
        // premier événement, pour ne jamais mélanger deux runs).
        onTraceReset();
        // Mode MCP (S7) : surface MCP-over-SSE (POST /mcp/sse, tool orchestrate)
        // — le canal privilégié quand MCP_FIRST=true gèle l'API HTTP legacy.
        if (mcpMode) {
          await askMcpTurn(assistantId, trimmed, controller);
          return;
        }
        // Mode Multi-agents : orchestration superviseur / workers (SSE agent.*).
        if (multiMode) {
          await askMultiAgentTurn(assistantId, trimmed, controller);
          return;
        }
        // Mode Agent (v2) : noyau agentique — boucle
        // Intent -> Plan -> Policy -> Budget -> Action.
        if (coreMode) {
          await askCoreTurn(assistantId, trimmed, controller);
          return;
        }

        const body: ChatRequestBody = { message: trimmed, history };
        // Conversation active (persistance serveur). Absent ou '' : le backend
        // crée une session à la volée (ou laisse l'échange hors journal).
        if (sessionId) body.session_id = sessionId;
        // Modèle choisi via le sélecteur de l'en-tête ('' = défaut serveur).
        if (selectedModel) body.model = selectedModel;
        // Mode « Réflexion » : champ backend en snake_case (enable_thinking) —
        // la forme camelCase serait ignorée par Pydantic.
        if (enableThinking) body.enable_thinking = true;
        // POST /api/v1/chat/ai est une route d'ACTION : session JWT prioritaire
        // (rôle admin requis), repli X-API-Key.
        const headers: Record<string, string> = {
          'Content-Type': 'application/json',
          ...resolveAuthHeaders(),
        };

        const base = resolveBaseUrl();
        const response = await fetch(`${base}${AI_ENDPOINT}`, {
          method: 'POST',
          headers,
          body: JSON.stringify(body),
          signal: controller.signal,
        });

        if (!response.ok) {
          throw new Error(await apiErrorMessage(response));
        }

        const contentType = response.headers.get('content-type') ?? '';

        if (contentType.includes('text/event-stream') && response.body) {
          // Mode streaming : chaque événement SSE enrichit la bulle au fil de l'eau.
          for await (const payload of readSseEvents(response.body)) {
            if (payload === '[DONE]') break;

            let event: ChatStreamEvent;
            try {
              event = JSON.parse(payload) as ChatStreamEvent;
            } catch {
              // Charge utile non JSON : affichée telle quelle (tolérance).
              appendDelta(assistantId, payload);
              continue;
            }

            if (event.error) throw new Error(event.error);
            if (event.thinking_delta) appendThinkingDelta(assistantId, event.thinking_delta);
            if (event.delta) appendDelta(assistantId, event.delta);
          }
        } else {
          // Repli : réponse JSON classique, non streamée.
          const data = (await response.json()) as { content?: string };
          appendDelta(assistantId, data.content ?? '');
        }
      } catch (error) {
        // Une annulation volontaire (bouton Stop) n'est pas une erreur.
        if (!controller.signal.aborted) {
          const detail = error instanceof Error ? error.message : String(error);
          patchMessage(assistantId, { error: detail });
        }
      } finally {
        // Décharge les fragments encore tamponnés AVANT la clôture du message
        // (sinon ils s'ajouteraient après thinkingStreaming=false).
        flushStreamBuffer();
        patchMessage(assistantId, { streaming: false, thinkingStreaming: false });
        setIsLoading(false);
        abortRef.current = null;
      }
    },
    [
      abortRef,
      appendDelta,
      appendThinkingDelta,
      askCoreTurn,
      askMcpTurn,
      askMultiAgentTurn,
      coreMode,
      enableThinking,
      flushStreamBuffer,
      isLoadingRef,
      messagesRef,
      mcpMode,
      multiMode,
      onTraceReset,
      patchMessage,
      selectedModel,
      sessionId,
      setIsLoading,
      setMessages,
    ],
  );

  return {
    sendMessage,
    askMcpTurn,
    askMultiAgentTurn,
    askCoreTurn,
    stopGeneration,
    flushStreamBuffer,
    patchMessage,
    activeAssistantIdRef,
  };
}
