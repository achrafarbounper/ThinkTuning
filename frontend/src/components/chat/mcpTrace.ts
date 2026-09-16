/**
 * mcpTrace.ts — L3 (SCRUM-154)
 * ---------------------------------------------------------------------
 * État de trace multi-agent PARTAGÉ + reducer pur `applyMcpEvent`.
 *
 * Le même réducteur alimente :
 *  - le hook React `useMultiAgentTrace` (temps réel, persistance localStorage) ;
 *  - ChatWindow (affichage dans la bulle d'assistant) ;
 *  - les tests unitaires (fonction pure, aucun effet de bord React).
 *
 * Enrichissements L3 par rapport à l'ancienne trace (multiPlan/multiWorkers) :
 *  - `run_id` durable du run MCP (curseur de replay, reprise) ;
 *  - `tools` : appels d'outils observés (orchestrate.tool / agent.worker.tool) ;
 *  - événements `skipped` (worker filtré par la politique d'intention) et
 *    `intent` (intention globale détectée par le superviseur) ;
 *  - `approvals` : actions d'approbation HITL traversées pendant le run.
 */

import type { McpOrchestrateEvent } from '../../api/mcpClient';
import type {
  MultiAgentPlanTask,
  MultiAgentWorkerState,
} from './types';

/** Une action d'approbation observée dans la trace (HITL). */
export interface TraceApprovalEntry {
  /** Identifiant de la demande d'approbation (request_id). */
  request_id?: string;
  /** Outil concerné. */
  tool?: string;
  /** Motif de validation. */
  reason?: string;
  /** Arguments tronqués de l'appel. */
  args?: unknown;
  /** Sous-tâche (worker) bloquée. */
  task_id?: string;
}

/** Une entrée d'outil dans la trace (timeline chronologique). */
export interface TraceToolEntry {
  tool: string;
  event: string;
  args?: unknown;
  status?: string;
  summary?: string;
  duration_ms?: number;
}

/** État complet de la trace multi-agent d'un tour. */
export interface MultiAgentTraceState {
  /** Identifiant DURABLE du run MCP (orchestrate.started / résultat). */
  runId?: string;
  /** Dernière séquence persistée côté serveur (curseur de replay). */
  lastSequence?: number;
  /** Plan validé par le superviseur (agent.plan). */
  plan?: MultiAgentPlanTask[];
  /** État des workers (agent.worker.*). */
  workers?: MultiAgentWorkerState[];
  /** Timeline des appels d'outils observés. */
  tools?: TraceToolEntry[];
  /** Intention globale détectée (agent.intent). */
  intent?: string;
  /** Workers filtrés par la politique d'intention (agent.worker.skipped). */
  skipped?: Array<{ worker_id?: string | null; reason?: string }>;
  /** Actions d'approbation HITL traversées. */
  approvals?: TraceApprovalEntry[];
  /** Notice d'orchestration (repli, timeout, deadline…). */
  notice?: string;
  /** Dernière erreur d'orchestration (agent.error / orchestrate.error). */
  error?: string;
  /** Réponse finale streamée (agent.done / final_answer). */
  finalAnswer?: string;
}

/**
 * Trace vide — état initial du hook et cible de `reset` (nouveau tour).
 * Objet gelé : aucun consommateur ne doit muter l'état initial partagé.
 */
export const EMPTY_MULTI_AGENT_TRACE: MultiAgentTraceState = Object.freeze({});

/**
 * Applique un événement MCP à l'état de trace — REDUCER PUR (state -> state).
 *
 * Retourne le même objet si l'événement ne concerne pas la trace (performance) ;
 * sinon une copie immuable mise à jour. Tolérant aux payloads partiels :
 * la trace ne doit JAMAIS être effacée par un événement incomplet (critère
 * L3 « les erreurs réseau ne doivent pas effacer silencieusement la trace »).
 */
export function applyMcpEvent(
  state: MultiAgentTraceState,
  event: McpOrchestrateEvent,
): MultiAgentTraceState {
  switch (event.kind) {
    case 'started': {
      // Le prélude arrive avant tout le reste ; run_id peut être null (store
      // durable indisponible) — on ne régresse JAMAIS sur un run_id connu.
      if (!event.run_id && state.runId && state.lastSequence !== undefined) {
        return state;
      }
      return {
        ...state,
        ...(event.run_id ? { runId: event.run_id } : {}),
        lastSequence: event.last_sequence,
      };
    }

    case 'intent':
      return {
        ...state,
        intent:
          event.intent ??
          (typeof event.payload.intent === 'string' ? event.payload.intent : state.intent),
      };

    case 'skipped':
      return {
        ...state,
        skipped: [
          ...(state.skipped ?? []),
          { worker_id: event.worker_id, reason: event.reason },
        ],
      };

    case 'multi_agent':
      return applyMultiAgentPayload(state, event.event, event.payload);

    case 'phase': {
      if (event.status !== 'timeout' && event.status !== 'error' && event.status !== 'failed') {
        return state;
      }
      const message =
        event.reason === 'synthesis_timeout'
          ? 'La synthèse a dépassé son délai ; les résultats partiels sont conservés.'
          : event.reason === 'orchestration_deadline_reached'
            ? 'La durée maximale de l’orchestration a été atteinte ; les résultats partiels sont conservés.'
            : 'Une phase de l’orchestration a échoué ; les résultats disponibles sont conservés.';
      return { ...state, notice: message };
    }

    case 'done':
      return applyMultiAgentPayload(state, 'agent.done', event.payload);

    case 'error':
      return { ...state, error: event.message || 'Échec de l’orchestration MCP.' };

    case 'fallback': {
      const reason = event.reason ?? 'capacité multi-agent indisponible';
      return {
        ...state,
        notice: `Le mode multi-agent MCP a été remplacé par le mode mono-agent (${reason}).`,
      };
    }

    case 'tool': {
      const tool = typeof event.tool.tool === 'string' ? event.tool.tool : undefined;
      if (!tool) return state;
      return {
        ...state,
        tools: [
          ...(state.tools ?? []),
          {
            tool,
            event: String(event.tool.event ?? 'tool_start'),
            ...(event.tool.args !== undefined ? { args: event.tool.args } : {}),
            ...(typeof event.tool.status === 'string' ? { status: event.tool.status } : {}),
            ...(typeof event.tool.summary === 'string' ? { summary: event.tool.summary } : {}),
            ...(typeof event.tool.duration_ms === 'number'
              ? { duration_ms: event.tool.duration_ms }
              : {}),
          },
        ],
      };
    }

    // thinking : affiché dans la bulle Réflexion (pas la trace) ;
    // rpc : réponse JSON-RPC finale traitée par ChatWindow.
    default:
      return state;
  }
}

/** Applique un payload d'orchestration brut (agent.* / orchestrate.*) à la trace. */
function applyMultiAgentPayload(
  state: MultiAgentTraceState,
  eventName: string,
  payload: Record<string, unknown>,
): MultiAgentTraceState {
  const taskId =
    typeof payload.task_id === 'string'
      ? payload.task_id
      : typeof payload.worker_id === 'string'
        ? payload.worker_id
        : undefined;

  switch (eventName) {
    case 'agent.plan': {
      const plan = payload.plan;
      if (Array.isArray(plan) && plan.length > 0) {
        return { ...state, plan: plan as MultiAgentPlanTask[] };
      }
      return state;
    }

    case 'orchestrate.worker':
    case 'agent.worker.start':
    case 'agent.worker.result':
    case 'agent.worker.error':
    case 'agent.worker.approval': {
      if (!taskId) return state;
      const status = String(payload.status ?? 'running');
      const workerStatus: MultiAgentWorkerState['status'] =
        status === 'error' || status === 'failed'
          ? 'error'
          : status === 'ok' || status === 'completed'
            ? 'ok'
            : status === 'awaiting_approval'
              ? 'awaiting_approval'
              : 'running';

      // Approbation HITL : mémorisée dans la trace (L3 — actions d'approbation).
      if (eventName === 'agent.worker.approval' || status === 'awaiting_approval') {
        const approvalPayload =
          payload.approval && typeof payload.approval === 'object'
            ? (payload.approval as TraceApprovalEntry)
            : undefined;
        state = {
          ...state,
          approvals: [
            ...(state.approvals ?? []),
            {
              ...(typeof payload.request_id === 'string'
                ? { request_id: payload.request_id }
                : {}),
              ...(approvalPayload?.tool ? { tool: approvalPayload.tool } : {}),
              ...(approvalPayload?.reason ? { reason: approvalPayload.reason } : {}),
              ...(approvalPayload?.args !== undefined ? { args: approvalPayload.args } : {}),
              task_id: taskId,
            },
          ],
        };
      }

      const existing = state.workers ?? [];
      const index = existing.findIndex((worker) => worker.task_id === taskId);
      if (index === -1) {
        return {
          ...state,
          workers: [
            ...existing,
            {
              task_id: taskId,
              role: String(payload.role ?? payload.worker_id ?? 'worker'),
              ...(typeof payload.subtask === 'string' ? { subtask: payload.subtask } : {}),
              status: workerStatus,
              ...(typeof payload.summary === 'string' ? { summary: payload.summary } : {}),
              ...(typeof payload.message === 'string' ? { message: payload.message } : {}),
              ...(typeof payload.duration_ms === 'number'
                ? { durationMs: payload.duration_ms }
                : {}),
            },
          ],
        };
      }
      const workers = [...existing];
      workers[index] = {
        ...workers[index],
        status: workerStatus,
        ...(typeof payload.summary === 'string' && payload.summary
          ? { summary: payload.summary }
          : {}),
        ...(typeof payload.message === 'string' ? { message: payload.message } : {}),
        ...(typeof payload.duration_ms === 'number'
          ? { durationMs: payload.duration_ms }
          : {}),
      };
      return { ...state, workers };
    }

    case 'agent.worker.tool': {
      // Outil exécuté DANS un worker : mémorisé dans la timeline partagée.
      const coreTool =
        payload.core_tool && typeof payload.core_tool === 'object'
          ? (payload.core_tool as Record<string, unknown>)
          : payload;
      const tool = typeof coreTool.tool === 'string' ? coreTool.tool : undefined;
      if (!tool) return state;
      return {
        ...state,
        tools: [
          ...(state.tools ?? []),
          {
            tool,
            event: String(coreTool.event ?? 'tool_start'),
            ...(coreTool.args !== undefined ? { args: coreTool.args } : {}),
            ...(typeof coreTool.status === 'string' ? { status: coreTool.status } : {}),
            ...(typeof coreTool.summary === 'string' ? { summary: coreTool.summary } : {}),
            ...(typeof coreTool.duration_ms === 'number'
              ? { duration_ms: coreTool.duration_ms }
              : {}),
          },
        ],
      };
    }

    case 'agent.done': {
      const answer =
        typeof payload.final_answer === 'string'
          ? payload.final_answer
          : typeof payload.answer === 'string'
            ? payload.answer
            : undefined;
      return answer ? { ...state, finalAnswer: answer } : state;
    }

    case 'agent.fallback': {
      const reason = typeof payload.reason === 'string' ? payload.reason : undefined;
      return {
        ...state,
        notice: `Repli conversationnel${reason ? ` (${reason})` : ''} : aucun worker exécuté.`,
      };
    }

    default:
      return state;
  }
}

/** Sérialise la trace pour localStorage (JSON stable). */
export function serializeTrace(state: MultiAgentTraceState): string {
  return JSON.stringify(state);
}

/** Désérialise une trace persistée (retour `EMPTY_MULTI_AGENT_TRACE` si invalide). */
export function deserializeTrace(raw: string | null): MultiAgentTraceState {
  if (!raw) return EMPTY_MULTI_AGENT_TRACE;
  try {
    const parsed = JSON.parse(raw) as MultiAgentTraceState;
    if (typeof parsed !== 'object' || parsed === null) return EMPTY_MULTI_AGENT_TRACE;
    return parsed;
  } catch {
    return EMPTY_MULTI_AGENT_TRACE;
  }
}