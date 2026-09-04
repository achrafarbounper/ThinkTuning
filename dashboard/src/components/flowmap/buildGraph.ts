/**
 * buildGraph.ts — Construction et évolution du graphe.
 *
 *  - `applyEvent` : réduit un événement SSE horodaté en mutations du graphe
 *    (création des nœuds agents, des arcs dispatch/outil/retour/erreur).
 *  - `reduceTimeline` / `initGraph` : réduisent une liste d'événements.
 *  - `demoTimeline` : génère une session multi-agents réaliste (preview sans
 *    backend) — chaque événement est horodaté pour servir le Replay.
 *  - `heatFactors` : agrégation statique pour le mode Heatmap.
 */

import {
  classifyRole,
  classifyTool,
  type AgentNodeData,
  type FlowEdgeData,
  type FlowEvent,
  type GraphState,
} from "./types";

let seq = 0;
/** Identifiant unique global pour les arcs d'outil (unicité même en replay). */
function nextSeq(): number {
  seq += 1;
  return seq;
}

function ensureNode(graph: GraphState, role: string): AgentNodeData {
  const id = `role:${role}`;
  let node = graph.nodes[id];
  if (!node) {
    const meta = classifyRole(role);
    node = {
      id,
      role,
      kind: meta.kind,
      color: meta.color,
      icon: meta.icon,
      status: "ready",
      toolCount: 0,
      calls: 0,
    };
    graph.nodes[id] = node;
    graph.nodeOrder.push(id);
  }
  return node;
}

/** Insère ou met à jour un arc, en agrégeant les occurrences (heatmap). */
function upsertEdge(
  graph: GraphState,
  edge: Partial<FlowEdgeData> &
    Pick<FlowEdgeData, "id" | "source" | "target" | "kind" | "label" | "color" | "category">,
): FlowEdgeData {
  const existing = graph.edges[edge.id];
  if (existing) {
    existing.count += 1;
    if (edge.totalDurationMs) existing.totalDurationMs += edge.totalDurationMs;
    if (!existing.args && edge.args) existing.args = edge.args;
    if (!existing.summary && edge.summary) existing.summary = edge.summary;
    if (edge.status) existing.status = edge.status;
    return existing;
  }
  const fresh: FlowEdgeData = {
    id: edge.id,
    source: edge.source,
    target: edge.target,
    kind: edge.kind,
    label: edge.label,
    tool: edge.tool,
    category: edge.category,
    color: edge.color,
    status: edge.status ?? "running",
    count: 1,
    totalDurationMs: edge.totalDurationMs ?? 0,
    args: edge.args,
    summary: edge.summary,
  };
  graph.edges[edge.id] = fresh;
  graph.edgeOrder.push(edge.id);
  return fresh;
}
/**
 * Réduit un événement dans le graphe et retourne les ids d'arcs dont le départ
 * d'impulsion doit être déclenché par le canvas.
 */
export function applyEvent(graph: GraphState, event: FlowEvent): string[] {
  graph.timeline.push(event);
  const pings: string[] = [];

  switch (event.t) {
    case "plan": {
      graph.plan = event.plan;
      ensureNode(graph, "planner");
      break;
    }
    case "worker.start": {
      const node = ensureNode(graph, event.role);
      node.calls += 1;
      node.status = "running";
      const edge = upsertEdge(graph, {
        id: `dispatch:${event.task_id}`,
        source: "role:planner",
        target: node.id,
        kind: "dispatch",
        label: "dispatch",
        color: "#22d3ee",
        category: "other",
        status: "running",
      });
      pings.push(edge.id);
      break;
    }
    case "tool.start": {
      const node = ensureNode(graph, event.role);
      const meta = classifyTool(event.tool);
      const edge = upsertEdge(graph, {
        id: `tool:${event.role}:${event.tool}:${nextSeq()}`,
        source: "role:planner",
        target: node.id,
        kind: "tool",
        label: event.tool,
        tool: event.tool,
        category: meta.category,
        color: meta.color,
        status: "running",
        args: event.args,
      });
      pings.push(edge.id);
      node.status = "running";
      graph.toolCalls += 1;
      break;
    }
    case "tool.result": {
      const node = ensureNode(graph, event.role);
      node.toolCount += 1;
      const meta = classifyTool(event.tool);
      const edge = upsertEdge(graph, {
        id: `tool:${event.role}:${event.tool}:${nextSeq()}`,
        source: "role:planner",
        target: node.id,
        kind: "tool",
        label: event.tool,
        tool: event.tool,
        category: meta.category,
        color: event.status === "error" ? "#f2545b" : meta.color,
        status: event.status,
        totalDurationMs: event.duration_ms ?? 0,
        summary: event.summary,
      });
      pings.push(edge.id);
      node.status = node.status === "running" ? "ok" : node.status;
      break;
    }
    case "worker.error": {
      const node = ensureNode(graph, event.role);
      node.status = "error";
      const edge = upsertEdge(graph, {
        id: `error:${event.task_id}`,
        source: node.id,
        target: "role:planner",
        kind: "error",
        label: event.error_code ?? "erreur",
        color: "#f2545b",
        category: "error",
        status: "error",
      });
      pings.push(edge.id);
      break;
    }
    case "worker.result": {
      const node = ensureNode(graph, event.role);
      node.status = "ok";
      const edge = upsertEdge(graph, {
        id: `return:${event.task_id}`,
        source: node.id,
        target: "role:planner",
        kind: "return",
        label: "retour",
        color: "#8b94a3",
        category: "other",
        status: "ok",
        totalDurationMs: event.duration_ms ?? 0,
      });
      pings.push(edge.id);
      break;
    }
    case "worker.approval": {
      // La policy exige une validation humaine : le run est suspendu, l'agent
      // attend la décision (carte Approuver / Refuser côté Assistant IA).
      // L'empreinte SHA-256 de l'action garantit la reprise exacte.
      const node = ensureNode(graph, event.role);
      node.status = "ready";
      graph.runStatus = "pending_approval";
      break;
    }
    case "synthesizing": {
      // Orchestration multi-agents : les workers ont rendu, la synthèse finale
      // est en cours.
      graph.runStatus = "synthesizing";
      break;
    }
    case "done": {
      graph.finalAnswer = event.answer;
      graph.runStatus = "completed";
      break;
    }
    case "error": {
      graph.error = event.message;
      graph.runStatus = "error";
      break;
    }
    default:
      break;
  }

  return pings;
}

/** Graphe initial (planificateur présent, prêt à recevoir les événements). */
export function initGraph(): GraphState {
  const graph: GraphState = {
    nodes: {},
    edges: {},
    edgeOrder: [],
    nodeOrder: [],
    plan: [],
    timeline: [],
    startedAt: 0,
    toolCalls: 0,
  };
  ensureNode(graph, "planner");
  return graph;
}

/** Réduit un tableau d'événements (démo, chargement, replay). */
export function reduceTimeline(events: FlowEvent[]): GraphState {
  const graph = initGraph();
  for (const e of events) applyEvent(graph, e);
  return graph;
}