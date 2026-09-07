/**
 * ledger.ts — Journal chronologique des outils exécutés (« Tool Ledger »).
 *
 * Réduit la timeline d'un graphe en liste ordonnée d'appels d'outils :
 *   - chaque `tool.start` OUVRE un enregistrement numéroté (ordre d'appel) ;
 *   - chaque `tool.result` referme l'appel apparié (statut, durée, sortie).
 *
 * Chaque entrée conserve le lien vers les arcs du graphe (`startEdgeId` /
 * `resultEdgeId`) et vers l'agent exécutant (`role`) : la sélection d'une
 * ligne met en évidence l'arc planner → agent dans le canvas.
 *
 * Le compteur d'arcs est un miroir strict de `applyEvent` (tool.start ET
 * tool.result incrémentent chacun `edgeSeq`, dans l'ordre de la timeline) :
 * les identifiants calculés correspondent exactement à ceux du graphe et la
 * réduction reste déterministe (replay / heatmap stables).
 */

import { classifyTool } from "./types";
import type { GraphState, ToolCallRecord } from "./types";

/** Clé d'appariement d'un appel : même sous-tâche, même agent, même outil. */
function callKey(taskId: string, role: string, tool: string): string {
  return `${taskId}|${role}|${tool}`;
}

/**
 * Construit le journal des outils exécutés d'un graphe, dans l'ordre d'appel.
 * Les appels sans `tool.result` (run en cours, timeline tronquée) restent
 * « running » ; un `tool.result` orphelin produit une entrée autonome.
 */
export function buildToolLedger(graph: GraphState): ToolCallRecord[] {
  const records: ToolCallRecord[] = [];
  /** Files d'appels ouverts par clé — les workers exécutent leurs outils en séquence. */
  const open = new Map<string, ToolCallRecord[]>();
  /** Miroir du séquenceur d'arcs du graphe (un incrément par événement d'outil). */
  let edgeSeq = 0;

  for (const event of graph.timeline) {
    if (event.t !== "tool.start" && event.t !== "tool.result") continue;
    edgeSeq += 1;
    const edgeId = `tool:${event.role}:${event.tool}:${edgeSeq}`;
    const key = callKey(event.task_id, event.role, event.tool);

    if (event.t === "tool.start") {
      const meta = classifyTool(event.tool);
      const record: ToolCallRecord = {
        seq: records.length + 1,
        taskId: event.task_id,
        role: event.role,
        tool: event.tool,
        category: meta.category,
        color: meta.color,
        status: "running",
        args: event.args,
        startedAt: event.at,
        startEdgeId: edgeId,
      };
      records.push(record);
      const queue = open.get(key) ?? [];
      queue.push(record);
      open.set(key, queue);
      continue;
    }

    // tool.result : referme l'appel ouvert correspondant (le plus ancien —
    // exécution séquentielle au sein d'un worker) ou crée une entrée
    // autonome si orphelin (timeline tronquée, replay partiel).
    const queue = open.get(key);
    const record = queue?.shift();
    if (queue && queue.length === 0) open.delete(key);
    if (record) {
      record.status = event.status;
      record.summary = event.summary;
      record.durationMs = event.duration_ms ?? Math.max(0, event.at - record.startedAt);
      record.endedAt = event.at;
      record.resultEdgeId = edgeId;
    } else {
      const meta = classifyTool(event.tool);
      records.push({
        seq: records.length + 1,
        taskId: event.task_id,
        role: event.role,
        tool: event.tool,
        category: meta.category,
        color: event.status === "error" ? "#f2545b" : meta.color,
        status: event.status,
        summary: event.summary,
        durationMs: event.duration_ms,
        // Résultat orphelin : l'unique arc connu sert de départ ET de résultat.
        startedAt: event.at,
        endedAt: event.at,
        startEdgeId: edgeId,
        resultEdgeId: edgeId,
      });
    }
  }

  return records;
}
