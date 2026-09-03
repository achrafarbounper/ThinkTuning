/**
 * flowApi.ts — Accès au flux SSE multi-agents en mode « full ».
 *
 * POST /api/agent/multi/ask/stream (mode « full » pour recevoir les événements
 * d'observabilité agent.worker.tool) → trames nominatives → FlowEvent horodatés
 * prêts à être réduits dans le graphe.
 */

import { readNamedSseEvents, type NamedSseEvent } from "../components/chat/streamSse";
import { resetSseCounter, sseToFlowEvent } from "../components/flowmap/events";
import type { FlowEvent } from "../components/flowmap/types";
import { DEFAULT_BASE_URL } from "./clientCore";

const MULTI_ASK_STREAM_ENDPOINT = "/api/agent/multi/ask/stream";
const API_CONFIG_KEY = "thinktuning.apiConfig";

/** Clé API (X-API-Key) persistée localement, si présente. */
function resolveApiKey(): string {
  try {
    const raw = window.localStorage.getItem(API_CONFIG_KEY);
    if (!raw) return "";
    const parsed = JSON.parse(raw) as { apiKey?: string };
    return parsed.apiKey || "";
  } catch {
    return "";
  }
}

/** Modèle LLM choisi dans le sélecteur du chat (persisté), si présent. */
function resolveModel(): string {
  try {
    const raw = window.localStorage.getItem("thinktuning.chatModel");
    if (raw) return raw;
  } catch {
    /* ignore */
  }
  return "";
}

export interface FlowStreamOptions {
  prompt?: string;
  model?: string;
  signal?: AbortSignal;
  onEvent: (ev: FlowEvent) => void;
}

/**
 * Ouvre le flux multi-agents et diffuse les FlowEvent normalisés, horodatés
 * (base `at` = ms depuis l'ouverture). Résout à la fin, ou lève en cas
 * d'événement « agent.error » (traduit en erreur HTTP précoce par l'API).
 */
export async function streamMultiFlow({ prompt, model, signal, onEvent }: FlowStreamOptions): Promise<void> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const apiKey = resolveApiKey();
  if (apiKey) headers["X-API-Key"] = apiKey;

  const controller = new AbortController();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener("abort", () => controller.abort(), { once: true });
  }

  const body: Record<string, unknown> = {
    prompt: prompt && prompt.trim() ? prompt.trim() : "Analyse ce sujet et produis une synthèse structurée.",
    mode: "full",
    parallel: true,
  };
  const chosenModel = model || resolveModel();
  if (chosenModel) body.model = chosenModel;

  // Base URL du dashboard (proxy Vite en dev, sinon config persistée).
  const base = DEFAULT_BASE_URL.replace(/\/+$/, "");
  const response = await fetch(`${base}${MULTI_ASK_STREAM_ENDPOINT}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    signal: controller.signal,
  });

  if (!response.ok || !response.body) {
    let detail = `Statut ${response.status} sur /multi/ask/stream`;
    try {
      const err = (await response.json()) as { detail?: string };
      if (err.detail) detail = String(err.detail);
    } catch {
      /* corps non-JSON */
    }
    throw new Error(detail);
  }

  resetSseCounter();
  const startedAt = performance.now();
  const roleSubtask = new Map<string, string>();

  for await (const frame of readNamedSseEvents(response.body)) {
    onSseFrame(frame, startedAt, roleSubtask, onEvent);
  }
}

function onSseFrame(
  frame: NamedSseEvent,
  startedAt: number,
  roleSubtask: Map<string, string>,
  onEvent: (ev: FlowEvent) => void,
) {
  const at = Math.max(0, performance.now() - startedAt);
  const ev = sseToFlowEvent(frame, at, roleSubtask);
  if (ev) onEvent(ev);
}

/* --- Sessions passées (persistance backend « Agent Flow Map ») ---------------------- */

/** Résumé d'une session enregistrée (liste GET /api/agent/flow). */
export interface FlowSessionSummary {
  id: string;
  prompt: string;
  model: string;
  status: string;
  answer_summary: string;
  error?: string | null;
  created_at: string;
  finished_at?: string | null;
  /** Compteurs pour la liste (fournis par le backend). */
  tool_calls: number;
  agents: string[];
}

/** Détail d'une session (GET /api/agent/flow/{id}) : timeline horodatée. */
export interface StoredFlowEvent {
  event: string;
  data: Record<string, unknown>;
  at_ms: number;
}

export interface FlowSessionDetail {
  id: string;
  prompt: string;
  model: string;
  status: string;
  answer_summary: string;
  error?: string | null;
  events: StoredFlowEvent[];
  created_at: string;
  finished_at?: string | null;
}

/** Enveloppe de la liste (miroir du contrat backend). */
interface FlowListResponse {
  flows: FlowSessionSummary[];
  statuses: string[];
}

function _apiHeaders(json = false): Record<string, string> {
  const headers: Record<string, string> = {};
  if (json) headers["Content-Type"] = "application/json";
  const apiKey = resolveApiKey();
  if (apiKey) headers["X-API-Key"] = apiKey;
  return headers;
}

/** Liste les sessions enregistrées (requête authentifiée GET /api/agent/flow). */
export async function listFlowSessions(): Promise<FlowSessionSummary[]> {
  const base = DEFAULT_BASE_URL.replace(/\/+$/, "");
  const response = await fetch(`${base}/api/agent/flow`, { headers: _apiHeaders() });
  if (!response.ok) {
    throw new Error(`Liste des sessions impossible (statut ${response.status}).`);
  }
  const data = (await response.json()) as FlowListResponse;
  return data.flows ?? [];
}

/** Récupère une session et la convertit en timeline `FlowEvent` rejouable. */
export async function getFlowSession(id: string): Promise<FlowEvent[]> {
  const base = DEFAULT_BASE_URL.replace(/\/+$/, "");
  const response = await fetch(`${base}/api/agent/flow/${encodeURIComponent(id)}`, {
    headers: _apiHeaders(),
  });
  if (!response.ok) {
    throw new Error(`Session introuvable ou injoignable (statut ${response.status}).`);
  }
  const detail = (await response.json()) as FlowSessionDetail;
  return flowEventsToTimeline(detail.events);
}

/** Convertit la timeline JSON persistée en événements de graphe (FlowEvent). */
export function flowEventsToTimeline(items: StoredFlowEvent[]): FlowEvent[] {
  // Prépare la map task_id → subtask à partir du plan (pour les worker.start).
  const subtask = new Map<string, string>();
  for (const it of items) {
    if (it.event === "agent.plan" && Array.isArray(it.data?.plan)) {
      for (const p of it.data.plan as Array<{ task_id?: string; subtask?: string }>) {
        if (p?.task_id && p?.subtask) subtask.set(String(p.task_id), String(p.subtask));
      }
    }
  }
  const out: FlowEvent[] = [];
  for (const it of items) {
    try {
      const ev = sseToFlowEvent(
        { event: it.event, data: JSON.stringify(it.data ?? {}) },
        it.at_ms ?? 0,
        subtask,
      );
      if (ev) out.push(ev);
    } catch {
      /* événement illisible : ignoré, ne casse pas la timeline */
    }
  }
  return out;
}