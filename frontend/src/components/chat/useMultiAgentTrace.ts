/**
 * useMultiAgentTrace.ts — L3 (SCRUM-154)
 * ---------------------------------------------------------------------
 * Hook React qui matérialise la trace multi-agent partagée.
 *
 * Wrappe le reducer PUR `applyMcpEvent` (mcpTrace.ts) avec :
 *  - un état React (useReducer — transitions immuables, rendu prévisible) ;
 *  - une PERSISTANCE localStorage : la trace est restaurée au montage et
 *    réécrite à chaque changement — un rechargement de page (ou la
 *    restauration d'une session de chat) conserve le run_id, les outils et
 *    les approbations déjà affichées ;
 *  - une API `handleMcpEvent` à brancher directement sur le callback
 *    `onEvent` de `orchestrateViaMcpStream`, et `reset` pour un nouveau tour.
 */

import { useCallback, useEffect, useReducer } from 'react';
import type { McpOrchestrateEvent } from '../../api/mcpClient';
import {
  applyMcpEvent,
  deserializeTrace,
  EMPTY_MULTI_AGENT_TRACE,
  serializeTrace,
} from './mcpTrace';
import type { MultiAgentTraceState } from './mcpTrace';

/** Clé localStorage de la dernière trace multi-agent (restauration de session). */
export const MULTI_AGENT_TRACE_STORAGE_KEY = 'thinktuning.multiAgentTrace';

/** Action du reducer interne : applique un événement MCP, ou réinitialise. */
type TraceAction =
  | { type: 'apply'; event: McpOrchestrateEvent }
  | { type: 'reset' }
  | { type: 'hydrate'; state: MultiAgentTraceState };

function traceReducer(
  state: MultiAgentTraceState,
  action: TraceAction,
): MultiAgentTraceState {
  switch (action.type) {
    case 'apply':
      return applyMcpEvent(state, action.event);
    case 'reset':
      return EMPTY_MULTI_AGENT_TRACE;
    case 'hydrate':
      return action.state;
    default:
      return state;
  }
}

export interface UseMultiAgentTraceResult {
  /** État courant de la trace (render-ready). */
  trace: MultiAgentTraceState;
  /** À brancher sur le callback `onEvent` d'`orchestrateViaMcpStream`. */
  handleMcpEvent: (event: McpOrchestrateEvent) => void;
  /** Réinitialise la trace (nouveau tour / nouvelle session). */
  reset: () => void;
  /** Restaure explicitement une trace (ex : rechargement de session). */
  hydrate: (state: MultiAgentTraceState) => void;
}

/**
 * Trace multi-agent temps réel + persistée. La persistance est
 * best-effort : un localStorage indisponible (navigation privée stricte)
 * dégrade en mémoire de session sans casser le chat.
 */
export function useMultiAgentTrace(): UseMultiAgentTraceResult {
  const [trace, dispatch] = useReducer(traceReducer, EMPTY_MULTI_AGENT_TRACE, (initial) => {
    // Restauration paresseuse : la dernière trace connue survit au
    // rechargement de la page (critère « persister la trace multi-agent
    // lors de la restauration des sessions »).
    try {
      return deserializeTrace(window.localStorage.getItem(MULTI_AGENT_TRACE_STORAGE_KEY));
    } catch {
      return initial;
    }
  });

  // Persistance à chaque changement (best-effort, jamais bloquante).
  useEffect(() => {
    try {
      if (Object.keys(trace).length === 0) {
        window.localStorage.removeItem(MULTI_AGENT_TRACE_STORAGE_KEY);
      } else {
        window.localStorage.setItem(
          MULTI_AGENT_TRACE_STORAGE_KEY,
          serializeTrace(trace),
        );
      }
    } catch {
      /* stockage indisponible : l'état en mémoire reste valable */
    }
  }, [trace]);

  const handleMcpEvent = useCallback((event: McpOrchestrateEvent) => {
    dispatch({ type: 'apply', event });
  }, []);

  const reset = useCallback(() => {
    dispatch({ type: 'reset' });
  }, []);

  const hydrate = useCallback((state: MultiAgentTraceState) => {
    dispatch({ type: 'hydrate', state });
  }, []);

  return { trace, handleMcpEvent, reset, hydrate };
}