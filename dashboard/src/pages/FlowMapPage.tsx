/**
 * FlowMapPage.tsx — Page « Agent Flow Map ».
 *
 * Visualisation animée du pipeline agentique :
 *   - Live    : lance un run et anime le graphe en temps réel — au choix
 *               l'orchestration multi-agents (SSE /multi/ask/stream, mode
 *               « full ») ou le noyau agentique v2 (SSE /ask/core/stream,
 *               un seul agent « noyau ») ;
 *   - Replay  : rejoue la timeline capturée (multi-agents OU noyau v2,
 *               sessions persistées « core.* ») ou la démo à 1×/2×/4× ;
 *   - Heatmap : agrégation statique (chaleur = sollicitation, rouge = erreurs).
 *
 * Interactions : clic nœud/outil → panneau latéral, zoom fluide, pan, cadrage,
 * filtres (agent / outil / statut).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import "../styles/flowmap.css";
import {
  getFlowSession,
  listFlowSessions,
  streamCoreFlow,
  streamMultiFlow,
  type FlowSessionSummary,
} from "../api/flowApi";
import { applyEvent, initGraph, reduceTimeline } from "../components/flowmap/buildGraph";
import { demoTimeline } from "../components/flowmap/demo";
import { buildToolLedger } from "../components/flowmap/ledger";
import { computeHeat } from "../components/flowmap/heat";
import { NO_FILTER, Toolbar, type FlowFilters } from "../components/flowmap/Toolbar";
import { FlowCanvas } from "../components/flowmap/FlowCanvas";
import { DetailPanel } from "../components/flowmap/DetailPanel";
import { type FlowMode, type FlowEvent, type FlowSource, type GraphState, type Pulse, type Selection } from "../components/flowmap/types";

let pulseSeq = 0;

export default function FlowMapPage() {
  const [mode, setMode] = useState<FlowMode>("live");
  // Source du run live : orchestration multi-agents ou noyau agentique v2.
  const [source, setSource] = useState<FlowSource>("multi");
  const [prompt, setPrompt] = useState("");
  const [running, setRunning] = useState(false);
  const [sourceEvents, setSourceEvents] = useState<FlowEvent[]>([]);
  const [replayCursor, setReplayCursor] = useState(0);
  const [replayPlaying, setReplayPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [pulses, setPulses] = useState<Pulse[]>([]);
  const [flowList, setFlowList] = useState<FlowSessionSummary[]>([]);
  const [flowLoadError, setFlowLoadError] = useState("");
  const [selected, setSelected] = useState<Selection>(null);
  const [filters, setFilters] = useState<FlowFilters>({ agent: NO_FILTER, tool: NO_FILTER, status: NO_FILTER, period: "all" });
  const [error, setError] = useState("");

  const liveGraphRef = useRef<GraphState>(initGraph());
  const prevEdgesRef = useRef<Set<string>>(new Set());
  const displayGraphRef = useRef<GraphState>(liveGraphRef.current);
  const abortRef = useRef<AbortController | null>(null);

  // Graphe d'affichage selon le mode.
  const displayGraph: GraphState =
    mode === "live"
      ? liveGraphRef.current
      : reduceTimeline(mode === "replay" ? sourceEvents.slice(0, replayCursor) : sourceEvents);
  displayGraphRef.current = displayGraph;

  // Détecte les nouveaux arcs (live & replay) et déclenche des impulsions.
  const edgeSig = displayGraph.edgeOrder.join("|");
  useEffect(() => {
    const ids = displayGraphRef.current.edgeOrder;
    const fresh = ids.filter((id) => !prevEdgesRef.current.has(id));
    prevEdgesRef.current = new Set(ids);
    if (!fresh.length) return;
    setPulses((prev) => [
      ...prev,
      ...fresh.map((edgeId) => ({
        id: `p${++pulseSeq}`,
        edgeId,
        color: displayGraphRef.current.edges[edgeId]?.color ?? "#22d3ee",
      })),
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [edgeSig]);

  const onPulseDone = (id: string) => setPulses((prev) => prev.filter((p) => p.id !== id));
/** Lance un run live (multi-agents /multi/ask/stream ou noyau v2 /ask/core/stream). */
  const runLive = async () => {
    if (running) {
      abortRef.current?.abort();
      return;
    }
    setError("");
    setSelected(null);
    liveGraphRef.current = initGraph();
    prevEdgesRef.current = new Set();
    setPulses([]);
    setSourceEvents([]);
    setRunning(true);

    const controller = new AbortController();
    abortRef.current = controller;
    const onEvent = (ev: FlowEvent) => {
      applyEvent(liveGraphRef.current, ev);
      // Le graphe est muté sur place : la copie superficielle change son
      // identité pour que les useMemo de la page (vue filtrée, journal des
      // outils) recalculent à CHAQUE événement du run live.
      liveGraphRef.current = { ...liveGraphRef.current };
      setSourceEvents((curr) => [...curr, ev]);
    };
    try {
      if (source === "core") {
        // Noyau agentique v2 : un seul agent « noyau », outils réels, policy
        // sandbox (les refus apparaissent comme arcs d'erreur).
        await streamCoreFlow({ prompt, signal: controller.signal, onEvent });
      } else {
        // Orchestration multi-agents : superviseur + workers spécialisés.
        await streamMultiFlow({ prompt, signal: controller.signal, onEvent });
      }
    } catch (err) {
      if (!controller.signal.aborted) {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setRunning(false);
      abortRef.current = null;
      refreshFlows(); // la session terminée est persistée côté serveur
    }
  };

  /** Charge une session (démo ou passée) et bascule en Replay. */
  const loadSession = (ev: FlowEvent[]) => {
    const full = reduceTimeline(ev);
    setError("");
    setSelected(null);
    liveGraphRef.current = full;
    setSourceEvents(ev);
    setReplayCursor(0);
    setReplayPlaying(false);
    setMode("replay");
    setPulses([]);
    // Ne pas pré-réserver les arcs : pendant le replay, chaque arc nouvellement
    // visible déclenchera sa propre impulsion (prévEdgesRef reste en phase).
    prevEdgesRef.current = new Set();
  };

  /** Charge la session de démonstration. */
  const loadDemo = () => loadSession(demoTimeline());

  /** Charge une session enregistrée côté backend (GET /api/agent/flow/{id}). */
  const handleLoadFlow = async (flowId: string) => {
    if (!flowId) return;
    setError("");
    setFlowLoadError("");
    try {
      const events = await getFlowSession(flowId);
      if (events.length === 0) {
        setError("Cette session est vide ou illisible.");
        return;
      }
      loadSession(events);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setFlowLoadError(msg);
      setError(msg);
    }
  };

  // Charge la liste des sessions enregistrées au montage (non bloquant).
  useEffect(() => {
    let cancelled = false;
    listFlowSessions()
      .then((flows) => {
        if (!cancelled) setFlowList(flows);
      })
      .catch(() => {
        // Backend injoignable ou non authentifié : la liste est simplement vide.
        if (!cancelled) setFlowLoadError("");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Recharge la liste des sessions (utile après un run live qui en persiste une).
  const refreshFlows = () => {
    listFlowSessions()
      .then((flows) => setFlowList(flows))
      .catch(() => {
        /* silencieux : on conserve l'état courant */
      });
  };

  const setModeAndReset = (m: FlowMode) => {
    setMode(m);
    if (m === "replay") setReplayCursor(sourceEvents.length);
    if (m === "live") setReplayPlaying(false);
  };

  // Lecture du replay : avance cursor, rythmé par l'écart inter-événement (borné)
  // divisé par la vitesse.
  useEffect(() => {
    if (mode !== "replay" || !replayPlaying) return;
    const events = sourceEvents;
    if (replayCursor >= events.length) {
      setReplayPlaying(false);
      return;
    }
    const prevAt = replayCursor === 0 ? 0 : events[replayCursor - 1].at;
    const nextAt = events[replayCursor].at;
    const dwell = Math.max(220, Math.min(900, (nextAt - prevAt) / 2)) / speed;
    const t = window.setTimeout(() => setReplayCursor((c) => Math.min(events.length, c + 1)), dwell);
    return () => window.clearTimeout(t);
  }, [mode, replayPlaying, replayCursor, speed, sourceEvents]);

  const onReplayToggle = () => {
    if (replayPlaying) {
      setReplayPlaying(false);
      return;
    }
    if (replayCursor >= sourceEvents.length) setReplayCursor(0);
    // Ré-base les impulsions sur les arcs déjà visibles à la position courante :
    // seuls les arcs qui apparaîtront ensuite animeront.
    prevEdgesRef.current = new Set(displayGraphRef.current.edgeOrder);
    setReplayPlaying(true);
  };

  const onSeek = (p: number) => {
    setReplayPlaying(false);
    setReplayCursor(Math.max(0, Math.min(sourceEvents.length, Math.floor(p * sourceEvents.length))));
  };

  const replayProgress = sourceEvents.length ? replayCursor / sourceEvents.length : 0;
// Options de filtres (agents, outils) issues du graphe courant.
  const agents = useMemo(
    () =>
      displayGraph.nodeOrder
        .map((id) => displayGraph.nodes[id])
        .filter((n) => n && n.id !== "role:planner")
        .map((n) => n.role),
    [displayGraph],
  );
  const tools = useMemo(() => {
    const set = new Set<string>();
    displayGraph.edgeOrder.forEach((id) => {
      const e = displayGraph.edges[id];
      if (e && e.tool) set.add(e.tool);
    });
    return Array.from(set);
  }, [displayGraph]);

  // Filtrage visuel : arcs puis nœuds atteignables (le planificateur reste).
  const visible = useMemo(() => {
    let edges = displayGraph.edgeOrder.map((id) => displayGraph.edges[id]);
    if (filters.tool !== NO_FILTER) {
      edges = edges.filter((e) => e.tool === filters.tool || e.label === filters.tool);
    }
    // Un filtre de statut d'ARC (running / ok / error) réduit les arcs ; un
    // filtre de statut de NŒUD (ex : awaiting_approval → nœud « awaiting »)
    // réduit les nœuds puis ne garde que les arcs qui les relient.
    const isNodeStatusFilter =
      filters.status !== NO_FILTER &&
      filters.status !== "running" &&
      filters.status !== "ok" &&
      filters.status !== "error";
    if (filters.status !== NO_FILTER && !isNodeStatusFilter) {
      edges = edges.filter((e) => e.status === filters.status);
    }
    if (filters.agent !== NO_FILTER) {
      edges = edges.filter((e) => {
        const agentId = e.source === "role:planner" ? e.target : e.source;
        return agentId === `role:${filters.agent}`;
      });
    }
    const known = new Set<string>();
    edges.forEach((e) => {
      known.add(e.source);
      known.add(e.target);
    });
    const nodes = displayGraph.nodeOrder
      .map((id) => displayGraph.nodes[id])
      .filter((n) => n && (n.id === "role:planner" || known.has(n.id)));
    let filteredNodes = nodes;
    if (isNodeStatusFilter) {
      const wanted = filters.status === "awaiting_approval" ? "awaiting" : filters.status;
      filteredNodes = nodes.filter((n) => n.status === wanted);
      const nodeIds = new Set(filteredNodes.map((n) => n.id));
      edges = edges.filter((e) => nodeIds.has(e.source) && nodeIds.has(e.target));
    }
    return { nodes: filteredNodes, edges };
  }, [displayGraph, filters]);

  // Journal des outils exécutés (ordre d'appel), filtré comme la vue canvas :
  // mêmes règles agent / outil / statut d'arc. Les statuts de NŒUD (ex :
  // awaiting_approval) ne s'appliquent pas aux appels d'outils.
  const ledger = useMemo(() => {
    let records = buildToolLedger(displayGraph);
    if (filters.tool !== NO_FILTER) records = records.filter((r) => r.tool === filters.tool);
    if (filters.agent !== NO_FILTER) records = records.filter((r) => r.role === filters.agent);
    if (filters.status === "running" || filters.status === "ok" || filters.status === "error") {
      records = records.filter((r) => r.status === filters.status);
    }
    return records;
  }, [displayGraph, filters]);

  const heat = mode === "heatmap" ? computeHeat(visible.nodes, visible.edges) : null;
  const focusRelated = selected != null;

  return (
    <div className="flowmap">
      <header className="page-head page-head--flow">
        <div className="pf-head">
          <div className="pf-head__titles">
            <h1>Agent Flow Map</h1>
            <p>
              Graphe orienté et animé du pipeline agentique : nœuds = agents (ou noyau v2),
              arcs = outils, impulsions = messages. Live (multi-agents ou noyau v2), Replay
              des sessions persistées et Heatmap.
            </p>
          </div>
          <StatusBar graph={displayGraph} running={running} mode={mode} source={source} />
        </div>

        {flowList.length > 0 && (
          <div className="fsessions">
            <label htmlFor="fsession-select">Session enregistrée :</label>
            <select
              id="fsession-select"
              value=""
              onChange={(e) => handleLoadFlow(e.target.value)}
              disabled={running}
            >
              <option value="">— Charger une session —</option>
              {flowList.map((s) => (
                <option key={s.id} value={s.id}>
                  {formatDate(s.created_at)} · {s.status} · {s.tool_calls} outil{s.tool_calls > 1 ? "s" : ""} ·{" "}
                  {(s.agents.length ? `${s.agents.length} agent${s.agents.length > 1 ? "s" : ""}` : "")}
                  {s.prompt ? ` · ${truncate(s.prompt, 48)}` : ""}
                </option>
              ))}
            </select>
          </div>
        )}
        {flowLoadError && <div className="fsessions__error">{flowLoadError}</div>}
      </header>

      <Toolbar
        mode={mode}
        setMode={setModeAndReset}
        source={source}
        setSource={setSource}
        prompt={prompt}
        setPrompt={setPrompt}
        onRunLive={runLive}
        onLoadDemo={loadDemo}
        running={running}
        enabled={sourceEvents.length > 0}
        replayPlaying={replayPlaying}
        onReplayToggle={onReplayToggle}
        speed={speed}
        setSpeed={setSpeed}
        replayProgress={replayProgress}
        onSeek={onSeek}
        filters={filters}
        setFilters={setFilters}
        agents={agents}
        tools={tools}
      />

      {error && (
        <div className="falert" role="alert">
          {error}
        </div>
      )}

      <div className={`fc-layout${selected ? " fc-layout--has-panel" : ""}`}>
        {visible.nodes.length === 0 ? (
          <div className="fempty">
            <p>
              Aucune session chargée. Lancez un run (multi-agents ou noyau v2) ou
              chargez la démo pour voir le pipeline s'animer.
            </p>
            <button type="button" className="fempty__demo" onClick={loadDemo}>
              Charger la démo
            </button>
          </div>
        ) : (
          <FlowCanvas
            nodes={visible.nodes}
            edges={visible.edges}
            pulses={pulses}
            onPulseDone={onPulseDone}
            selected={selected}
            onSelect={setSelected}
            heat={heat}
            focusRelated={focusRelated}
            ledger={ledger}
          />
        )}
        <DetailPanel selected={selected} graph={displayGraph} onClose={() => setSelected(null)} />
      </div>

      <Legend heat={heat} />
    </div>
  );
}
/** Bandeau de résumé : statut du run, volume d'appels d'outils, mode. */
function StatusBar({
  graph,
  running,
  mode,
  source,
}: {
  graph: GraphState;
  running: boolean;
  mode: FlowMode;
  source: FlowSource;
}) {
  const modeLabel = mode === "live" ? "Temps réel" : mode === "replay" ? "Replay" : "Heatmap";
  const sourceLabel = source === "core" ? "Noyau v2" : "Multi-agents";
  // Libellés lisibles des statuts de la machine à états du run (domaine v2).
  // « awaiting_approval » est le statut canonique (aligné flow_store /
  // orchestrateur) — « resuming » rend la reprise native visible.
  const statusLabels: Record<string, string> = {
    completed: "Terminé",
    error: "Erreur",
    awaiting_approval: "Validation requise",
    resuming: "Reprise en cours…",
    synthesizing: "Synthèse en cours…",
  };
  const dotClass =
    running || graph.runStatus === "resuming" || graph.runStatus === "synthesizing"
      ? "fdot fdot--running"
      : graph.runStatus === "awaiting_approval"
        ? "fdot fdot--awaiting"
        : graph.runStatus === "error"
          ? "fdot fdot--error"
          : "fdot fdot--ok";
  return (
    <div className="fstatus">
      <span className="fstatus__item">
        <span className={dotClass} />
        {running ? "Run en cours" : statusLabels[graph.runStatus ?? ""] ?? "Prêt"}
      </span>
      <span className="fstatus__item">
        Outils : <strong>{graph.toolCalls}</strong>
      </span>
      <span className="fstatus__item">
        Agents : <strong>{graph.nodeOrder.length}</strong>
      </span>
      <span className="fstatus__item">Mode : {modeLabel}</span>
      <span className="fstatus__item">Source : {sourceLabel}</span>
      {graph.error && (
        <span className="fstatus__item fstatus__item--error">{graph.error}</span>
      )}
    </div>
  );
}

/** Légende des couleurs : catégories d'outils (+ chaleur en heatmap). */
function Legend({ heat }: { heat: ReturnType<typeof computeHeat> | null }) {
  const items: Array<[string, string, string]> = [
    ["Recherche", "#38bdf8", "search"],
    ["Code", "#a78bfa", "code"],
    ["Transformation", "#34d399", "transform"],
    ["Autre", "#8b94a3", "other"],
    ["Erreur", "#f2545b", "error"],
    ["Dispatch / retour", "#22d3ee", "dash"],
  ];
  return (
    <div className="flegend" aria-label="Légende des couleurs">
      {items.map(([label, color, key]) => (
        <span key={key} className="flegend__item">
          <span className="flegend__swatch" style={{ background: color }} />
          {label}
        </span>
      ))}
      {heat && (
        <span className="flegend__item flegend__heat">
          Nœuds chauds = agents les plus sollicités · arêtes épaisses = outils les
          plus utilisés
        </span>
      )}
    </div>
  );
}
/** Formate une date ISO en heure locale courte (liste des sessions). */
function formatDate(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${pad(d.getDate())}/${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch {
    return iso;
  }
}

/** Tronque un texte avec une ellipse. */
function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}