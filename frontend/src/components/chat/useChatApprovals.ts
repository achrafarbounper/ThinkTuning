/**
 * useChatApprovals.ts — L3 (SCRUM-154)
 * ---------------------------------------------------------------------
 * Décisions humaines (HITL) extraites de ChatWindow : APPROUVER et
 * REFUSER une action en attente (carte Approuver / Refuser).
 *
 * L'approbation passe par le canal HTTP whitelisté (/api/v1/agent/
 * approvals) — non bloqué par MCP_FIRST — puis le run interrompu est
 * relancé via resume_request_id dans le canal d'ORIGINE de la demande :
 *  - origin 'multi' → reprise NATIVE dans le même worker (multi/ask/stream) ;
 *  - origin 'mcp'   → reprise du run MCP durable (resume_request_id +
 *    run_id + task_id — P0 SCRUM-151) ;
 *  - origin 'core'  → noyau v2 mono-agent (fallback documenté).
 */

import { useCallback } from 'react';
import type { Dispatch, MutableRefObject, SetStateAction } from 'react';
import type { ChatMessageData, PendingApprovalData } from './types';
import {
  APPROVALS_ENDPOINT,
  apiErrorMessage,
  createId,
  nowIso,
  resolveAuthHeaders,
  resolveBaseUrl,
} from './chatTransport';

/** Dépendances du hook d'approbation (injectées par ChatWindow). */
export interface ChatApprovalsDeps {
  /** Demande d'approbation affichée (carte) — null si aucune. */
  pendingApproval: PendingApprovalData | null;
  /** Vrai pendant un tour (les boutons sont désactivés). */
  isLoading: boolean;
  setPendingApproval: (approval: PendingApprovalData | null) => void;
  setMessages: Dispatch<SetStateAction<ChatMessageData[]>>;
  setIsLoading: Dispatch<SetStateAction<boolean>>;
  /** Contrôleur d'interruption courant (bouton Stop). */
  abortRef: MutableRefObject<AbortController | null>;
  /** Tours de reprise (un par canal d'origine). */
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
  patchMessage: (id: string, patch: Partial<ChatMessageData>) => void;
  /** Décharge les fragments SSE coalescés avant la clôture de la bulle. */
  flushStreamBuffer: () => void;
}

/** API du hook d'approbation. */
export interface ChatApprovalsApi {
  handleApprove: () => Promise<void>;
  handleReject: () => Promise<void>;
}

export function useChatApprovals(deps: ChatApprovalsDeps): ChatApprovalsApi {
  const {
    pendingApproval,
    isLoading,
    setPendingApproval,
    setMessages,
    setIsLoading,
    abortRef,
    askMcpTurn,
    askMultiAgentTurn,
    askCoreTurn,
    patchMessage,
    flushStreamBuffer,
  } = deps;
  return useApprovalsApi({
    pendingApproval,
    isLoading,
    setPendingApproval,
    setMessages,
    setIsLoading,
    abortRef,
    askMcpTurn,
    askMultiAgentTurn,
    askCoreTurn,
    patchMessage,
    flushStreamBuffer,
  });
}

/** Construit les callbacks d'approbation (nommé `use*` : contient des hooks). */
function useApprovalsApi(deps: ChatApprovalsDeps): ChatApprovalsApi {
  const {
    pendingApproval,
    isLoading,
    setPendingApproval,
    setMessages,
    setIsLoading,
    abortRef,
    askMcpTurn,
    askMultiAgentTurn,
    askCoreTurn,
    patchMessage,
    flushStreamBuffer,
  } = deps;

  /**
   * APPROUVER : la demande passe à « approved » côté store, puis le run
   * interrompu est relancé via resume_request_id — l'action est exécutée
   * UNE fois et l'agent conclut.
   */
  const handleApprove = useCallback(async () => {
    if (!pendingApproval || isLoading) return;
    const { requestId, prompt } = pendingApproval;
    setPendingApproval(null);
    setIsLoading(true);

    const assistantId = createId();
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      // Session JWT prioritaire, repli X-API-Key (cf. resolveAuthHeaders).
      const headers: Record<string, string> = resolveAuthHeaders();
      const base = resolveBaseUrl();
      const response = await fetch(`${base}${APPROVALS_ENDPOINT}/${requestId}/approve`, {
        method: 'POST',
        headers,
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error(await apiErrorMessage(response));
      }

      // Reprise du run : nouvelle bulle, alimentée par la suite du run.
      // - origin 'multi' → REPRISE NATIVE : l'action approuvée est rejouée
      //   DANS le même worker via /multi/ask/stream + resume_request_id
      //   (empreinte SHA-256 revérifiée côté orchestrateur).
      // - origin 'core' (fallback documenté) → noyau v2 mono-agent.
      // - origin 'mcp' (S7) → REPRISE RÉELLE du MÊME run MCP :
      //   resume_request_id + run_id + task_id (P0 SCRUM-151).
      setMessages((previous) => [
        ...previous,
        {
          id: assistantId,
          role: 'assistant',
          content: '',
          createdAt: nowIso(),
          streaming: true,
        },
      ]);
      if (pendingApproval.origin === 'multi') {
        await askMultiAgentTurn(assistantId, prompt, controller, requestId, pendingApproval.taskId);
      } else if (pendingApproval.origin === 'mcp') {
        await askMcpTurn(
          assistantId,
          prompt,
          controller,
          requestId,
          pendingApproval.runId,
          pendingApproval.taskId,
        );
      } else {
        await askCoreTurn(assistantId, prompt, controller, requestId);
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        patchMessage(assistantId, {
          error: error instanceof Error ? error.message : String(error),
          streaming: false,
        });
      }
    } finally {
      // Clôture inconditionnelle (même sur interruption Stop).
      flushStreamBuffer();
      patchMessage(assistantId, { streaming: false });
      setIsLoading(false);
      abortRef.current = null;
    }
  }, [
    abortRef,
    askCoreTurn,
    askMcpTurn,
    askMultiAgentTurn,
    flushStreamBuffer,
    isLoading,
    patchMessage,
    pendingApproval,
    setIsLoading,
    setMessages,
    setPendingApproval,
  ]);

  /**
   * REFUSER : aucune exécution ; un message explicite trace le refus dans
   * la conversation.
   */
  const handleReject = useCallback(async () => {
    if (!pendingApproval || isLoading) return;
    const { requestId, tool, reason } = pendingApproval;
    setPendingApproval(null);
    setIsLoading(true);

    // Annulable via le bouton Stop (même convention que handleApprove).
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const headers: Record<string, string> = resolveAuthHeaders();
      const base = resolveBaseUrl();
      const response = await fetch(`${base}${APPROVALS_ENDPOINT}/${requestId}/reject`, {
        method: 'POST',
        headers,
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error(await apiErrorMessage(response));
      }
      setMessages((previous) => [
        ...previous,
        {
          id: createId(),
          role: 'assistant' as const,
          content: `[Action refusée] « ${tool} » n'a pas été exécutée. Motif du contrôle : ${reason}.`,
          createdAt: nowIso(),
        },
      ]);
    } catch (error) {
      // Une annulation volontaire (bouton Stop) n'est pas une erreur.
      if (!controller.signal.aborted) {
        setMessages((previous) => [
          ...previous,
          {
            id: createId(),
            role: 'assistant' as const,
            content: '',
            createdAt: nowIso(),
            error: error instanceof Error ? error.message : String(error),
          },
        ]);
      }
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
      }
      setIsLoading(false);
    }
  }, [abortRef, isLoading, pendingApproval, setIsLoading, setMessages, setPendingApproval]);

  return { handleApprove, handleReject };
}
