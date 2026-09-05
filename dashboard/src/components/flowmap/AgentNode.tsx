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

  return (
    <g
      transform={`translate(${centerX}, ${centerY})`}
      className={[
        "fnode",
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
      {/* Halo pulsant en exécution */}
      <rect
        className="fnode__halo"
        x={-6}
        y={-6}
        width={NODE_W + 12}
        height={NODE_H + 12}
        rx={18}
        fill="none"
        stroke={borderColor}
        strokeWidth={2}
      />
      {/* Corps du nœud */}
      <rect
        className="fnode__body"
        width={NODE_W}
        height={NODE_H}
        rx={14}
        fill="rgba(18, 22, 28, 0.92)"
        stroke={borderColor}
        strokeWidth={selected ? 2 : 1.25}
      />

      {/* Icône */}
      <g transform={`translate(26, ${NODE_H / 2})`}>
        <circle r={18} fill={borderColor} fillOpacity={0.18} stroke={borderColor} strokeWidth={1.25} />
        <text textAnchor="middle" dominantBaseline="central" fontSize={17}>
          {node.icon}
        </text>
      </g>

      {/* Nom du rôle */}
      <text
        className="fnode__name"
        x={54}
        y={NODE_H / 2 - 4}
        fill="#e7eaee"
        fontSize={13}
        fontWeight={600}
        fontFamily="Space Grotesk, sans-serif"
      >
        {truncate(node.role, 22)}
      </text>

      {/* Compteur d'outils */}
      <g transform={`translate(54, ${NODE_H / 2 + 13})`} className="fnode__meta">
        <circle r={3} fill={STATUS_DOT[node.status]} />
        <text x={8} dy={3.5} fontSize={10.5} fill="#8b94a3" fontFamily="IBM Plex Mono, monospace">
          {node.toolCount} outil{node.toolCount > 1 ? "s" : ""} · {node.calls} appels
        </text>
      </g>

      {/* Badge de chaleur (heatmap) */}
      {heatLabel && heatColor && (
        <text
          className="fnode__heat"
          x={NODE_W - 10}
          y={14}
          textAnchor="end"
          fontSize={10}
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