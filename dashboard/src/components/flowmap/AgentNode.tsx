/**
 * AgentNode.tsx — Nœud « agent » (rendu SVG).
 *
 * Bloc arrondi teinté de la couleur de son type, icône, nom, puce de statut,
 * compteur d'outils et un halo néon animé pendant l'exécution (CSS). En mode
 * heatmap, le fond est recoloré selon sa température.
 */

import { NODE_H, NODE_W } from "./geometry";
import type { AgentNodeData } from "./types";

interface AgentNodeProps {
  node: AgentNodeData;
  x: number;
  y: number;
  selected: boolean;
  dimmed: boolean;
  /** Couleur heatmap (le cas échéant) ; sinon null. */
  heatColor: string | null;
  heatLabel: boolean;
  onSelect: (id: string) => void;
}

const STATUS_DOT: Record<AgentNodeData["status"], string> = {
  ready: "#8b94a3",
  running: "#22d3ee",
  awaiting: "#d29922",
  ok: "#2fbf71",
  error: "#f2545b",
};

export function AgentNode({
  node,
  x,
  y,
  selected,
  dimmed,
  heatColor,
  heatLabel,
  onSelect,
}: AgentNodeProps) {
  const borderColor = heatColor ?? node.color;
  const centerX = x - NODE_W / 2;
  const centerY = y - NODE_H / 2;
  const running = node.status === "running";
  const isPlanner = node.id === "role:planner";

  return (
    <g
      transform={`translate(${centerX}, ${centerY})`}
      className={[
        "fnode",
        isPlanner ? "fnode--planner" : "",
        running ? "fnode--running" : "",
        node.status === "error" ? "fnode--error" : "",
        node.status === "awaiting" ? "fnode--awaiting" : "",
        selected ? "fnode--selected" : "",
        dimmed ? "fnode--dimmed" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      opacity={dimmed ? 0.35 : 1}
      onClick={(e) => {
        e.stopPropagation();
        onSelect(node.id);
      }}
      role="button"
      aria-label={`Agent ${node.role}`}
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect(node.id);
        }
      }}
    >
      {/* Halo pulsant en exécution (plus large pour l'orchestrateur) */}
      <rect
        className="fnode__halo"
        x={-7}
        y={-7}
        width={NODE_W + 14}
        height={NODE_H + 14}
        rx={20}
        fill="none"
        stroke={borderColor}
        strokeWidth={isPlanner ? 2.5 : 2}
      />
      {/* Corps du nœud */}
      <rect
        className="fnode__body"
        width={NODE_W}
        height={NODE_H}
        rx={16}
        fill={isPlanner ? "rgba(22, 30, 46, 0.96)" : "rgba(18, 22, 28, 0.92)"}
        stroke={borderColor}
        strokeWidth={selected ? 2.2 : isPlanner ? 1.8 : 1.3}
      />

      {/* Icône */}
      <g transform={`translate(30, ${NODE_H / 2})`}>
        <circle r={isPlanner ? 21 : 19} fill={borderColor} fillOpacity={0.18} stroke={borderColor} strokeWidth={1.25} />
        <text textAnchor="middle" dominantBaseline="central" fontSize={isPlanner ? 20 : 18}>
          {node.icon}
        </text>
      </g>

      {/* Nom du rôle */}
      <text
        className="fnode__name"
        x={62}
        y={NODE_H / 2 - 6}
        fill="#e7eaee"
        fontSize={isPlanner ? 15 : 14}
        fontWeight={isPlanner ? 700 : 600}
        fontFamily="Space Grotesk, sans-serif"
      >
        {truncate(node.role, 24)}
      </text>

      {/* Compteur d'outils */}
      <g transform={`translate(62, ${NODE_H / 2 + 16})`} className="fnode__meta">
        <circle r={3.5} fill={STATUS_DOT[node.status]} />
        <text x={9} dy={4} fontSize={11} fill="#8b94a3" fontFamily="IBM Plex Mono, monospace">
          {node.toolCount} outil{node.toolCount > 1 ? "s" : ""} · {node.calls} appels
        </text>
      </g>

      {/* Badge de chaleur (heatmap) */}
      {heatLabel && heatColor && (
        <text
          className="fnode__heat"
          x={NODE_W - 12}
          y={16}
          textAnchor="end"
          fontSize={11}
          fill="#e7eaee"
          fontFamily="IBM Plex Mono, monospace"
        >
          {node.toolCount}
        </text>
      )}
    </g>
  );
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}