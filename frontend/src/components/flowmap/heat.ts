/**
 * heat.ts — Agrégations statiques du mode « Heatmap ».
 *
 * Convertit le graphe (après réduction complète d'une session) en facteurs de
 * chaleur : nœuds les plus sollicités -> teintes chaudes, arcs les plus
 * fréquents -> plus épais, erreurs -> zones rouges.
 */

import type { AgentNodeData, FlowEdgeData } from "./types";

export interface HeatMetrics {
  nodeColor: (id: string) => string;
  edgeColor: (id: string) => string;
  edgeWidth: (id: string) => number;
  /** Ids des arcs d'erreur (affichés en rouge opaque). */
  errorEdges: string[];
}

const HEAT_STOPS = ["#1a2332", "#2f3f63", "#5b4f8f", "#8f4f9f", "#d94fa5", "#ff6b6b"];

function mix(a: string, b: string, f: number): string {
  const pa = parseInt(a.slice(1), 16);
  const pb = parseInt(b.slice(1), 16);
  const r = Math.round(((pa >> 16) & 255) + (((pb >> 16) & 255) - ((pa >> 16) & 255)) * f);
  const g = Math.round(((pa >> 8) & 255) + (((pb >> 8) & 255) - ((pa >> 8) & 255)) * f);
  const bl = Math.round((pa & 255) + ((pb & 255) - (pa & 255)) * f);
  return `rgb(${r}, ${g}, ${bl})`;
}

function lerpColor(stops: string[], t: number): string {
  const clamped = Math.max(0, Math.min(1, t));
  const seg = clamped * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(seg));
  const f = seg - i;
  return mix(stops[i], stops[i + 1], f);
}

/**
 * Calcule les facteurs de chaleur et expose les fonctions de style heatmap.
 * Les nœuds et arcs sont supposés correspondre à une session complète.
 */
export function computeHeat(
  nodes: AgentNodeData[],
  edges: FlowEdgeData[],
): HeatMetrics {
  const maxCalls = Math.max(1, ...nodes.map((n) => n.toolCount));
  const maxCount = Math.max(1, ...edges.map((e) => e.count));
  const nodeHeat: Record<string, number> = {};
  for (const n of nodes) nodeHeat[n.id] = maxCalls ? n.toolCount / maxCalls : 0;
  const edgeHeat: Record<string, number> = {};
  const errorEdges: string[] = [];
  for (const e of edges) {
    edgeHeat[e.id] = maxCount ? e.count / maxCount : 0;
    if (e.kind === "error" || e.status === "error") errorEdges.push(e.id);
  }
  return {
    errorEdges,
    nodeColor: (id) => lerpColor(HEAT_STOPS, nodeHeat[id] ?? 0),
    edgeColor: (id) =>
      errorEdges.includes(id) ? "#f2545b" : lerpColor(HEAT_STOPS, edgeHeat[id] ?? 0),
    edgeWidth: (id) => 1.5 + 3.5 * (edgeHeat[id] ?? 0),
  };
}