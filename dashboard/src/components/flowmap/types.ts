/**
 * types.ts — Modèle de données du « Agent Flow Map ».
 *
 * Le graphe est construit à partir des événements SSE de
 * `POST /api/agent/multi/ask/stream` (mode « full »). Un nœud = un agent
 * (le planificateur + les rôles des workers) ; un arc = un appel (dispatch,
 * outil, retour, erreur). L'ensemble est rejouable (timeline horodatée).
 */

/* --- Événements SSE (charge utile normalisée) -------------------------------- */

/** Une sous-tâche du plan validé par le superviseur (événement `agent.plan`). */
export interface PlanTask {
  task_id: string;
  role: string;
  subtask: string;
}

/**
 * Événement normalisé et horodaté. `at` = millisecondes écoulées depuis le
 * début de la session (base pour le Replay et la chronologie).
 */
export type FlowEvent =
  | { t: "plan"; at: number; plan: PlanTask[] }
  | { t: "worker.start"; at: number; task_id: string; role: string; subtask?: string }
  | { t: "tool.start"; at: number; task_id: string; role: string; tool: string; args?: string }
  | {
      t: "tool.result";
      at: number;
      task_id: string;
      role: string;
      tool: string;
      status: "ok" | "error";
      summary?: string;
      duration_ms?: number;
    }
  | { t: "worker.error"; at: number; task_id: string; role: string; error_code?: string; message?: string }
  | { t: "worker.result"; at: number; task_id: string; role: string; summary?: string; duration_ms?: number }
  | {
      t: "worker.approval";
      at: number;
      task_id: string;
      role: string;
      request_id?: string;
      message?: string;
      /** Décision de policy en attente : outil ciblé, raison, empreinte SHA-256. */
      approval?: { tool?: string; reason?: string; args_hash?: string };
    }
  | { t: "synthesizing"; at: number; worker_errors?: number }
  /**
   * Reprise native (FSM orchestrateur : awaiting_approval → resuming) : le
   * worker `role`/`task_id` est re-dispatché avec `request_id` (resume_request_id).
   */
  | { t: "resuming"; at: number; request_id: string; task_id: string; role: string }
  /**
   * Terminaison. `status` est le statut RÉEL émis par l'orchestrateur (ex :
   * "awaiting_approval" quand la synthèse a été interdite) — jamais forcé à
   * "completed" côté graphe (invariant FSM).
   */
  | { t: "done"; at: number; status?: string; answer?: string; duration_ms?: number }
  | { t: "error"; at: number; message?: string };

/* --- Typologie des agents ---------------------------------------------------- */

export type AgentKind = "planner" | "retrieval" | "code" | "transform" | "ui" | "other";

export interface AgentKindMeta {
  kind: AgentKind;
  label: string;
  /** Couleur principale du nœud (bleu nuit / accents néon). */
  color: string;
  icon: string;
}

/** Classifie un rôle de worker (les noms courants de l'orchestration). */
export function classifyRole(role: string): AgentKindMeta {
  const r = role.toLowerCase();
  if (r.includes("planner") || r.includes("orchestr") || r.includes("supervis")) {
    return { kind: "planner", label: "Planificateur", color: "#8b5cf6", icon: "🧠" };
  }
  if (r.includes("search") || r.includes("retriev") || r.includes("rag") || r.includes("reader") || r.includes("web")) {
    return { kind: "retrieval", label: "Recherche", color: "#38bdf8", icon: "📚" };
  }
  if (r.includes("code") || r.includes("exec") || r.includes("dev") || r.includes("python") || r.includes("shell")) {
    return { kind: "code", label: "Code", color: "#a78bfa", icon: "⚙️" };
  }
  if (r.includes("ui") || r.includes("front") || r.includes("design") || r.includes("render")) {
    return { kind: "ui", label: "UI", color: "#22d3ee", icon: "🎨" };
  }
  if (r.includes("transform") || r.includes("summar") || r.includes("extract") || r.includes("clean") || r.includes("convert")) {
    return { kind: "transform", label: "Transformation", color: "#34d399", icon: "🔁" };
  }
  return { kind: "other", label: "Agent", color: "#8b94a3", icon: "🤖" };
}

/* --- Typologie des arcs (outils) --------------------------------------------- */

export type ToolCategory = "search" | "code" | "transform" | "error" | "other";

export interface ToolCategoryMeta {
  category: ToolCategory;
  /** Couleur de l'arc dans le flux (spécification) et son libellé. */
  color: string;
  label: string;
}

/** Classifie un outil par catégorie (recherche / code / transformation / erreur). */
export function classifyTool(tool: string): ToolCategoryMeta {
  const t = tool.toLowerCase();
  if (t.includes("search") || t.includes("web") || t.includes("read") || t.includes("http") || t.includes("fetch") || t.includes("request") || t.includes("lookup") || t.includes("database") || t.includes("sql") || t.includes("select")) {
    return { category: "search", color: "#38bdf8", label: "Recherche" };
  }
  if (t.includes("exec") || t.includes("shell") || t.includes("command") || t.includes("run") || t.includes("write") || t.includes("edit") || t.includes("code") || t.includes("python") || t.includes("compile")) {
    return { category: "code", color: "#a78bfa", label: "Code" };
  }
  if (t.includes("convert") || t.includes("transform") || t.includes("summar") || t.includes("calc") || t.includes("format") || t.includes("extract") || t.includes("clean") || t.includes("dedupe") || t.includes("count")) {
    return { category: "transform", color: "#34d399", label: "Transformation" };
  }
  return { category: "other", color: "#8b94a3", label: "Outil" };
}

/* --- Graphe -------------------------------------------------------------- */

export type NodeStatus = "ready" | "running" | "awaiting" | "ok" | "error";

/** Décision de policy suspendue à une validation humaine (affichée au panel). */
export interface PendingApproval {
  /** Outil dont l'exécution est bloquée (ex : write_file). */
  tool?: string;
  /** Raison de la policy (pourquoi la validation est requise). */
  reason?: string;
  /** Empreinte SHA-256 de l'action — garantit une reprise exacte. */
  args_hash?: string;
  /** Identifiant de reprise (resume_request_id à renvoyer après approbation). */
  request_id?: string;
}

export interface AgentNodeData {
  id: string;
  /** Rôle (ex : « planner », « code_search »). */
  role: string;
  kind: AgentKind;
  color: string;
  icon: string;
  status: NodeStatus;
  /** Demande de validation humaine en cours (statut « awaiting »). */
  pendingApproval?: PendingApproval;
  /** Nombre d'outils exécutés par cet agent. */
  toolCount: number;
  /** Nombre total de sous-tâches/opérations. */
  calls: number;
}

export type EdgeKind = "dispatch" | "tool" | "return" | "error";

export type EdgeStatus = "running" | "ok" | "error";

export interface FlowEdgeData {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
  /** Libellé : nom de l'outil, « dispatch » ou « retour ». */
  label: string;
  tool?: string;
  category: ToolCategory;
  color: string;
  status: EdgeStatus;
  count: number;
  /** Durée cumulée (ms) — moyenne affichée = total / count. */
  totalDurationMs: number;
  args?: string;
  summary?: string;
}

/** État agrégé du graphe (muté par la réduction des événements SSE). */
export interface GraphState {
  nodes: Record<string, AgentNodeData>;
  edges: Record<string, FlowEdgeData>;
  /** Ordre d'insertion des arcs (chronologie). */
  edgeOrder: string[];
  nodeOrder: string[];
  plan: PlanTask[];
  timeline: FlowEvent[];
  startedAt: number;
  finalAnswer?: string;
  runStatus?: string;
  error?: string;
  toolCalls: number;
  /**
   * Séquenceur interne des identifiants d'arcs d'outil.
   * Réduit dans le graphe (et non partagé entre appels) : unique en son sein
   * (live) et DÉTERMINISTE pour une même timeline — deux `reduceTimeline(events)`
   * produisent des arcs identiques. Un compteur module-global incrémenté sans
   * remise à zéro faisait changer les ids à chaque rendu du Replay/Heatmap et
   * déclenchait une boucle infinie de `setPulses` (« Maximum update depth »).
   */
  edgeSeq?: number;
}

/** Impulsion lumineuse traversant un arc (id unique + couleur néon). */
export interface Pulse {
  id: string;
  edgeId: string;
  color: string;
}

/** Sélection courante (nœud ou arc) → panneau latéral. */
export type Selection = { type: "node"; id: string } | { type: "edge"; id: string } | null;

/** Modes d'affichage : temps réel, replay chronologique, heatmap statique. */
export type FlowMode = "live" | "replay" | "heatmap";

/**
 * Source d'un run Live : orchestration multi-agents (POST /api/agent/multi/
 * ask/stream) ou noyau agentique v2 (POST /api/agent/ask/core/stream — un
 * seul agent « noyau », mêmes événements d'outils).
 */
export type FlowSource = "multi" | "core";

export const EMPTY_GRAPH: GraphState = {
  nodes: {},
  edges: {},
  edgeOrder: [],
  nodeOrder: [],
  plan: [],
  timeline: [],
  startedAt: 0,
  toolCalls: 0,
};