/**
 * events.ts — Traduction des trames SSE nommées en événements de graphe.
 *
 * Consomme les trames produites par `readNamedSseEvents` (POST
 * /api/agent/multi/ask/stream, mode « full ») et les normalise en `FlowEvent`
 * horodatés (base `at` = ms écoulées depuis le début de session).
 */

import type { FlowEvent, PlanTask } from "./types";

/** Cache de la plage horaire : les tools_start arrivent sans durée globale. */
let seqCounter = 0;

export interface SseFrame {
  event: string;
  data: string;
}

/** Toolkit pour rendre les identifiants de sous-tâches stables. */
function taskId(role: string, n: number): string {
  return `${role}-${n}`;
}

/**
 * Convertit une trame SSE nominative en FlowEvent (ou `null` si hors graphe).
 * `at` est chronologique ; `startMs` fournit la base temporelle de session.
 */
export function sseToFlowEvent(
  frame: SseFrame,
  at: number,
  roleSubtask: Map<string, string>,
): FlowEvent | null {
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(frame.data) as Record<string, unknown>;
  } catch {
    return null;
  }

  switch (frame.event) {
    case "agent.plan": {
      const rawPlan = (payload.plan as Array<Record<string, unknown>> | undefined) ?? [];
      const plan: PlanTask[] = rawPlan.map((p) => ({
        task_id: String(p.task_id ?? ""),
        role: String(p.role ?? "?") || "?",
        subtask: String(p.subtask ?? ""),
      }));
      return { t: "plan", at, plan };
    }
    case "agent.worker.start": {
      const role = String(payload.role ?? "?");
      const tid = String(payload.task_id ?? taskId(role, ++seqCounter));
      const subtask = Array.isArray(payload.plan)
        ? (payload.plan[0] as Record<string, unknown>)?.subtask
        : undefined;
      if (typeof subtask === "string") roleSubtask.set(tid, subtask);
      return { t: "worker.start", at, task_id: tid, role, subtask: roleSubtask.get(tid) };
    }
    case "agent.worker.tool": {
      const sub = (payload.event as string | undefined) ?? "tool_start";
      const role = String(payload.role ?? "?");
      const tid = String(payload.task_id ?? "");
      const tool = String(payload.tool ?? "");
      if (sub === "tool_start" || sub === "tool_start") {
        return {
          t: "tool.start",
          at,
          task_id: tid,
          role,
          tool,
          args: payload.args ? JSON.stringify(payload.args) : undefined,
        };
      }
      return {
        t: "tool.result",
        at,
        task_id: tid,
        role,
        tool,
        status: payload.status === "error" ? "error" : "ok",
        summary: payload.summary ? String(payload.summary) : undefined,
        duration_ms: typeof payload.duration_ms === "number" ? payload.duration_ms : undefined,
      };
    }
    case "agent.worker.result":
      return {
        t: "worker.result",
        at,
        task_id: String(payload.task_id ?? ""),
        role: String(payload.role ?? "?"),
        summary: payload.summary ? String(payload.summary) : undefined,
        duration_ms: typeof payload.duration_ms === "number" ? payload.duration_ms : undefined,
      };
    case "agent.worker.error":
      return {
        t: "worker.error",
        at,
        task_id: String(payload.task_id ?? ""),
        role: String(payload.role ?? "?"),
        error_code: payload.error_code ? String(payload.error_code) : undefined,
        message: payload.message ? String(payload.message) : undefined,
      };
    case "agent.worker.approval":
      return {
        t: "worker.approval",
        at,
        task_id: String(payload.task_id ?? ""),
        role: String(payload.role ?? "?"),
        request_id: payload.request_id ? String(payload.request_id) : undefined,
        message: payload.message ? String(payload.message) : undefined,
      };
    case "agent.synthesizing":
      return {
        t: "synthesizing",
        at,
        worker_errors: typeof payload.worker_errors === "number" ? payload.worker_errors : undefined,
      };
    case "agent.done":
      return {
        t: "done",
        at,
        answer: payload.answer ? String(payload.answer) : payload.final_answer ? String(payload.final_answer) : undefined,
        duration_ms: typeof payload.duration_ms === "number" ? payload.duration_ms : undefined,
      };
    case "agent.error":
      return { t: "error", at, message: payload.message ? String(payload.message) : undefined };
    default:
      return null;
  }
}

/** Réinstancie le compteur (utile entre deux sessions). */
export function resetSseCounter(): void {
  seqCounter = 0;
}
