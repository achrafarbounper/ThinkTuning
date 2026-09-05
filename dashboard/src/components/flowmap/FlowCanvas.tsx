/**
 * FlowCanvas.tsx — Toile interactive du « Agent Flow Map ».
 * Rendu SVG : zoom façon Figma/Miro, pan par glisser, fit-view auto,
 * impulsions lumineuses (rAF), halo néon sur nœuds/arcs actifs.
 */

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type MutableRefObject,
  type SetStateAction,
  type WheelEvent as ReactWheelEvent,
} from "react";
import { AgentNode } from "./AgentNode";
import { FlowEdge } from "./FlowEdge";
import { arrowGeometry, computeBounds, computeLayout, edgePath, returnPath, type ArrowGeometry } from "./geometry";
import type { HeatMetrics } from "./heat";
import type { AgentNodeData, FlowEdgeData, Pulse, Selection } from "./types";

const IMPULSE_MS = 900;
const MIN_ZOOM = 0.3;
const MAX_ZOOM = 3;
/** Zoom plancher du cadrage auto : autorise à sortir du graphe entier, même petit. */
const FIT_MIN_ZOOM = 0.12;

interface View {
  x: number;
  y: number;
  k: number;
}

interface FlowCanvasProps {
  nodes: AgentNodeData[];
  edges: FlowEdgeData[];
  pulses: Pulse[];
  onPulseDone: (id: string) => void;
  selected: Selection;
  onSelect: (sel: Selection) => void;
  heat: HeatMetrics | null;
  focusRelated: boolean;
}
export function FlowCanvas({
  nodes,
  edges,
  pulses,
  onPulseDone,
  selected,
  onSelect,
  heat,
  focusRelated,
}: FlowCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const pathMap = useRef<Map<string, SVGPathElement>>(new Map());
  const progressRef = useRef<Map<string, number>>(new Map());
  const pulsesRef = useRef<Pulse[]>(pulses);
  pulsesRef.current = pulses;
  const onDoneRef = useRef(onPulseDone);
  onDoneRef.current = onPulseDone;

  const [view, setView] = useState<View>({ x: 0, y: 0, k: 1 });
  const [, setFrame] = useState(0);
  const [hoveredEdge, setHoveredEdge] = useState<string | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const dragRef = useRef({ startX: 0, startY: 0, viewX: 0, viewY: 0, active: false });

  const layout = useMemo(() => computeLayout(nodes, nodes.map((n) => n.id)), [nodes]);
  const edgeGeos = useMemo(
    () =>
      edges.map((edge) => {
        const src = layout.positions[edge.source];
        const tgt = layout.positions[edge.target];
        if (!src || !tgt) return null;
        const incoming = edge.kind === "return" || edge.kind === "error";
        const d = incoming ? returnPath(src, tgt) : edgePath(src, tgt);
        return {
          edge,
          d,
          arrow: arrowGeometry(src, tgt, incoming),
          labelPos: { x: (src.x + tgt.x) / 2, y: (src.y + tgt.y) / 2 },
        };
      }),
    [edges, layout],
  );

  // Densité de la vue : rappel discret de l'ampleur du flux (agents / outils / arcs).
  const density = useMemo(() => {
    const toolArcs = edges.filter((e) => e.kind === "tool").length;
    return { agents: nodes.length, tools: toolArcs, arcs: edges.length };
  }, [nodes, edges]);

  const fitView = useMemo(
    () => () => {
      const el = containerRef.current;
      if (!el) return;
      const b = computeBounds(layout.positions);
      const cw = el.clientWidth;
      const ch = el.clientHeight;
      if (cw <= 0 || ch <= 0) return;
      // Le cadrage auto priorise l'affichage du graphe complet (~90 % du viewport :
      // belle marge de respiration), sans se bloquer au zoom plancher manuel.
      const k = Math.max(FIT_MIN_ZOOM, Math.min(MAX_ZOOM * 0.9, Math.min(cw / (b.maxX - b.minX), ch / (b.maxY - b.minY))));
      setView({ x: cw / 2 - ((b.minX + b.maxX) / 2) * k, y: ch / 2 - ((b.minY + b.maxY) / 2) * k, k });
    },
    [layout],
  );

  const nodeCount = nodes.length;
  useEffect(() => {
    fitView();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeCount]);

  /** Bascule la toile en plein écran : simple changement d'état, l'API Fullscreen
   * est pilotée en effet (accès au DOM hors rendu, conforme react-hooks/refs). */
  const toggleFullscreen = () => setIsFullscreen((v) => !v);
  const fsSupported =
    typeof document !== "undefined" &&
    typeof document.documentElement.requestFullscreen === "function";

  useEffect(() => {
    const el = containerRef.current;
    if (!el || !fsSupported) return;
    if (isFullscreen && !document.fullscreenElement) {
      const p = el.requestFullscreen?.();
      if (p && typeof (p as Promise<void>).catch === "function") {
        (p as Promise<void>).catch(() => setIsFullscreen(false));
      }
    } else if (!isFullscreen && document.fullscreenElement) {
      const p = document.exitFullscreen?.();
      if (p && typeof (p as Promise<void>).catch === "function") {
        (p as Promise<void>).catch(() => undefined);
      }
    }
  }, [isFullscreen, fsSupported]);
  // Synchronise l'état interne avec l'API (échappement Échap, changement d'onglet…).
  useEffect(() => {
    const onFsChange = () => setIsFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", onFsChange);
    return () => document.removeEventListener("fullscreenchange", onFsChange);
  }, []);
  // Au passage en plein écran : re-cadre le graphe dans la nouvelle taille.
  useEffect(() => {
    if (!isFullscreen) return;
    const t = window.setTimeout(() => fitView(), 80);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isFullscreen]);

  // Animation des impulsions (rAF, sans re-rendu quand tout est au repos).
  useEffect(() => {
    let raf = 0;
    let last = performance.now();
    const step = (now: number) => {
      const dt = Math.min(120, now - last);
      last = now;
      const actives = pulsesRef.current;
      if (actives.length) {
        const done: string[] = [];
        for (const p of actives) {
          const pr = Math.min(1, (progressRef.current.get(p.id) ?? 0) + dt / IMPULSE_MS);
          progressRef.current.set(p.id, pr);
          if (pr >= 1) done.push(p.id);
        }
        setFrame((f) => f + 1);
        for (const id of done) onDoneRef.current(id);
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, []);

  const storePath = (edgeId: string) => (el: SVGPathElement | null) => {
    if (el) pathMap.current.set(edgeId, el);
    else pathMap.current.delete(edgeId);
  };
return (
    <div
      ref={containerRef}
      className="fc"
      onPointerDown={(e) => {
        dragRef.current = { startX: e.clientX, startY: e.clientY, viewX: view.x, viewY: view.y, active: true };
      }}
      onPointerMove={(e) => {
        const d = dragRef.current;
        if (!d.active) return;
        setView((v) => ({ ...v, x: d.viewX + (e.clientX - d.startX), y: d.viewY + (e.clientY - d.startY) }));
      }}
      onPointerUp={() => {
        dragRef.current.active = false;
      }}
      onPointerCancel={() => {
        dragRef.current.active = false;
      }}
      onWheel={(e) => handleWheel(e, setView)}
      onDoubleClick={() => fitView()}
    >
      <svg ref={svgRef} className="fc__svg" width="100%" height="100%">
        <defs>
          <pattern id="fc-grid" width="40" height="40" patternUnits="userSpaceOnUse">
            <path d="M 40 0 L 0 0 0 40" fill="none" stroke="rgba(139,148,163,0.08)" strokeWidth="1" />
          </pattern>
        </defs>
        <g transform={`translate(${view.x}, ${view.y}) scale(${view.k})`}>
          <rect x={-2000} y={-2000} width={4000} height={4000} fill="url(#fc-grid)" />
          {renderGraph({
            edgeGeos,
            pathMap,
            storePath,
            pulses,
            progressRef,
            selected,
            onSelect,
            hoveredEdge,
            setHoveredEdge,
            heat,
            focusRelated,
            nodes,
            layout,
          })}
        </g>
      </svg>

      {/* Densité du graphe : lecture immédiate de l'ampleur du flux. */}
      <div className="fc__badges" aria-live="polite">
        <span className="fc__badge">{density.agents} agent{density.agents > 1 ? "s" : ""}</span>
        <span className="fc__badge fc__badge--tools">{density.tools} outil{density.tools > 1 ? "s" : ""}</span>
        <span className="fc__badge fc__badge--arcs">{density.arcs} arc{density.arcs > 1 ? "s" : ""}</span>
      </div>

      {/* Aide au déplacement : interactions disponibles sans documentation. */}
      <div className="fc__hint">Glisser = déplacer · Molette = zoom · Double-clic = cadrer</div>

      {renderControls(view, setView, fitView, isFullscreen, fsSupported, toggleFullscreen)}
    </div>
  );
}

interface GraphRenderArgs {
  edgeGeos: Array<{ edge: FlowEdgeData; d: string; arrow: ArrowGeometry; labelPos: { x: number; y: number } } | null>;
  pathMap: MutableRefObject<Map<string, SVGPathElement>>;
  storePath: (edgeId: string) => (el: SVGPathElement | null) => void;
  pulses: Pulse[];
  progressRef: MutableRefObject<Map<string, number>>;
  selected: Selection;
  onSelect: (sel: Selection) => void;
  hoveredEdge: string | null;
  setHoveredEdge: Dispatch<SetStateAction<string | null>>;
  heat: HeatMetrics | null;
  focusRelated: boolean;
  nodes: AgentNodeData[];
  layout: { positions: Record<string, { x: number; y: number }> };
}
function renderGraph(args: GraphRenderArgs) {
  const { edgeGeos, pathMap, storePath, pulses, progressRef, selected, onSelect, hoveredEdge, setHoveredEdge, heat, focusRelated, nodes, layout } = args;
  const selId = selected?.id ?? null;
  const selType = selected?.type ?? null;

  const relatedNodes = new Set<string>();
  const relatedEdges = new Set<string>();
  if (selType === "node") {
    // Le nœud sélectionné reste lumineux (et ses voisins par arcs).
    relatedNodes.add(selId!);
  }
  edgeGeos.forEach((geo) => {
    if (!geo) return;
    if (selType === "node" && geo.edge.source === selId) {
      relatedEdges.add(geo.edge.id);
      relatedNodes.add(geo.edge.target);
    }
    if (selType === "edge" && geo.edge.id === selId) {
      relatedEdges.add(geo.edge.id);
      relatedNodes.add(geo.edge.source);
      relatedNodes.add(geo.edge.target);
    }
  });

  return (
    <>
      {edgeGeos.map((geo) => {
        if (!geo) return null;
        const { edge, d, labelPos, arrow } = geo;
        const color = heat ? heat.edgeColor(edge.id) : edge.color;
        const width = heat ? heat.edgeWidth(edge.id) : baseWidth(edge);
        const related = selType != null && !relatedEdges.has(edge.id);
        return (
          <FlowEdge
            key={edge.id}
            edge={edge}
            d={d}
            labelPos={labelPos}
            arrow={arrow}
            color={color}
            strokeWidth={width}
            dimmed={focusRelated && related}
            selected={edge.id === selId}
            hovered={hoveredEdge === edge.id}
            pathRef={storePath(edge.id)}
            onSelect={(id) => onSelect({ type: "edge", id })}
            onHover={(id) => setHoveredEdge(id)}
          />
        );
      })}

      {/* Impulsions lumineuses en transit le long des arcs. */}
      {pulses.map((p) => {
        const path = pathMap.current.get(p.edgeId);
        if (!path) return null;
        const prog = progressRef.current.get(p.id) ?? 0;
        const pt = path.getPointAtLength(prog * path.getTotalLength());
        return <circle key={p.id} cx={pt.x} cy={pt.y} r={prog > 0.92 ? 5 : 4} fill={p.color} className="fimpulse" />;
      })}

      {nodes.map((node) => {
        const pos = layout.positions[node.id];
        if (!pos) return null;
        const related = selType != null && !relatedNodes.has(node.id);
        return (
          <AgentNode
            key={node.id}
            node={node}
            x={pos.x}
            y={pos.y}
            selected={node.id === selId}
            dimmed={focusRelated && related}
            heatColor={heat ? heat.nodeColor(node.id) : null}
            heatLabel={heat != null}
            onSelect={(id) => onSelect({ type: "node", id })}
          />
        );
      })}
    </>
  );
}

function baseWidth(edge: FlowEdgeData): number {
  // Hiérarchie visuelle des arcs : outil (porteur d'information) > dispatch/retour.
  if (edge.kind === "tool") return edge.status === "error" ? 2.4 : 1.9;
  if (edge.kind === "error") return 1.7;
  return 1.35;
}

function handleWheel(e: ReactWheelEvent<HTMLDivElement>, setView: Dispatch<SetStateAction<View>>) {
  e.preventDefault();
  const rect = (e.currentTarget as HTMLDivElement).getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
  const factor = Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0015));
  setView((v) => {
    const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, v.k * factor));
    const kf = k / v.k;
    return { k, x: mx - (mx - v.x) * kf, y: my - (my - v.y) * kf };
  });
}

function renderControls(
  view: View,
  setView: Dispatch<SetStateAction<View>>,
  fit: () => void,
  isFullscreen: boolean,
  fsSupported: boolean,
  toggleFullscreen: () => void,
) {
  const zoom = (f: number) =>
    setView((v) => {
      const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, v.k * f));
      return { ...v, k };
    });
  return (
    <div className="fc__zoom" aria-label="Contrôles du zoom et du pleine écran">
      <button type="button" title="Zoom avant" onClick={() => zoom(1.25)}>+</button>
      <span className="fc__zoom-value">{Math.round(view.k * 100)}%</span>
      <button type="button" title="Zoom arrière" onClick={() => zoom(0.8)}>−</button>
      <span className="fc__zoom-divider" aria-hidden="true" />
      <button type="button" title="Cadrer la vue" onClick={fit}>⌖</button>
      {fsSupported && (
        <button
          type="button"
          className={isFullscreen ? "is-active" : ""}
          title={isFullscreen ? "Quitter le plein écran" : "Agrandir la toile (plein écran)"}
          onClick={toggleFullscreen}
          aria-pressed={isFullscreen}
        >
          {isFullscreen ? "✕" : "⛶"}
        </button>
      )}
    </div>
  );
}