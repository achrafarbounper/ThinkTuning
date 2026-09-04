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
      if (sub === "tool_start") {
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
    /* --- Noyau v2 (/api/agent/ask/core/stream) : sessions enregistrées ------- */
    case "core.start":
      return {
        t: "worker.start",
        at,
        task_id: "core",
        role: String(payload.role ?? "noyau") || "noyau",
        subtask: payload.prompt ? String(payload.prompt) : undefined,
      };
    case "core.tool": {
      const sub = (payload.event as string | undefined) ?? "tool_start";
      const tool = String(payload.tool ?? "");
      if (sub === "tool_start") {
        return {
          t: "tool.start",
          at,
          task_id: "core",
          role: "noyau",
          tool,
          args: payload.args ? JSON.stringify(payload.args) : undefined,
        };
      }
      return {
        t: "tool.result",
        at,
        task_id: "core",
        role: "noyau",
        tool,
        status: payload.status === "error" ? "error" : "ok",
        summary: payload.summary ? String(payload.summary) : undefined,
        duration_ms: typeof payload.duration_ms === "number" ? payload.duration_ms : undefined,
      };
    }
    case "core.approval":
      return {
        t: "worker.approval",
        at,
        task_id: "core",
        role: "noyau",
        request_id: payload.request_id ? String(payload.request_id) : undefined,
        message: payload.message ? String(payload.message) : undefined,
      };
    case "core.done":
      return {
        t: "done",
        at,
        answer: payload.answer ? String(payload.answer) : undefined,
      };
    case "core.error":
      return { t: "error", at, message: payload.message ? String(payload.message) : undefined };
    default:
      return null;
  }
}

/* --- Noyau v2 en LIVE (frames SSE non nommées de /ask/core/stream) ------- */

/**
 * Charge utile d'une frame SSE du noyau v2 (POST /api/agent/ask/core/stream).
 * Contrairement au flux multi-agents, les frames sont NON nommées : le champ
 * discriminant est la clé de l'objet JSON (core_tool / thinking_delta / delta
 * / final / error), avec la sentinelle `data: [DONE]`.
 */
export interface CoreStreamFrame {
  /** Appel d'outil regroupé (tool_start puis tool_result), comme en stocké. */
  core_tool?: {
    event?: string;
    tool: string;
    args?: Record<string, unknown>;
    status?: string;
    summary?: string;
    error?: string;
    duration_ms?: number;
  };
  thinking_delta?: string;
  delta?: string;
  /** Réponse finale du noyau (même contrat AskResponse que /ask/core). */
  final?: {
    response: string;
    model?: string;
    status: string;
    request_id?: string | null;
    approval?: { tool?: string; reason?: string; args?: Record<string, unknown> } | null;
  };
  error?: string;
}

/**
 * Traduit une frame SSE live du noyau v2 en FlowEvent, ou `null` si la frame
 * ne concerne pas le graphe (deltas texte, thinking, [DONE], JSON illisible).
 * Convention identique aux sessions stockées : rôle « noyau », task_id « core ».
 */
export function coreFrameToFlowEvent(payload: string, at: number): FlowEvent | null {
  if (!payload || payload === "[DONE]") return null;
  let frame: CoreStreamFrame;
  try {
    frame = JSON.parse(payload) as CoreStreamFrame;
  } catch {
    return null;
  }

  if (frame.core_tool) {
    const tool = String(frame.core_tool.tool ?? "");
    if (!tool) return null;
    if ((frame.core_tool.event ?? "tool_start") === "tool_start") {
      return {
        t: "tool.start",
        at,
        task_id: "core",
        role: "noyau",
        tool,
        args: frame.core_tool.args ? JSON.stringify(frame.core_tool.args) : undefined,
      };
    }
    return {
      t: "tool.result",
      at,
      task_id: "core",
      role: "noyau",
      tool,
      status: frame.core_tool.status === "error" ? "error" : "ok",
      summary: frame.core_tool.summary
        ? String(frame.core_tool.summary)
        : frame.core_tool.error
          ? String(frame.core_tool.error)
          : undefined,
      duration_ms:
        typeof frame.core_tool.duration_ms === "number" ? frame.core_tool.duration_ms : undefined,
    };
  }

  if (frame.final) {
    const status = String(frame.final.status ?? "completed");
    if (status === "awaiting_approval") {
      return {
        t: "worker.approval",
        at,
        task_id: "core",
        role: "noyau",
        request_id: frame.final.request_id ? String(frame.final.request_id) : undefined,
        message: frame.final.approval?.reason
          ? String(frame.final.approval.reason)
          : "Policy : validation humaine requise",
      };
    }
    if (status === "completed") {
      return { t: "done", at, answer: String(frame.final.response ?? "") };
    }
    // rejected / error : le run s'est terminé sur un refus de policy (règle
    // dure ou anti-boucle) ou une panne — l'explication est dans la réponse.
    return { t: "error", at, message: String(frame.final.response ?? status) };
  }

  if (frame.error) {
    return { t: "error", at, message: String(frame.error) };
  }

  // thinking_delta / delta : texte streamé (déjà rejoué par « final »),
  // sans impact sur le graphe.
  return null;
}

/** Réinstancie le compteur (utile entre deux sessions). */
export function resetSseCounter(): void {
  seqCounter = 0;
}
