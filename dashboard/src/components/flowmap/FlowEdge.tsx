/**
 * FlowEdge.tsx — Arc « tool » (rendu SVG).
 *
 * Ligne fine (animé pendant l'exécution), étiquette flottante = nom de l'outil,
 * couleur par catégorie (recherche / code / transformation / erreur). Le survol
 * durcit l'arc ; le clic épingle sa fiche (input/output/durée).
 */

import type { FlowEdgeData } from "./types";

interface FlowEdgeProps {
  edge: FlowEdgeData;
  d: string;
  /** Position centrale approximative pour l'étiquette. */
  labelPos: { x: number; y: number };
  color: string;
  strokeWidth: number;
  dimmed: boolean;
  selected: boolean;
  hovered: boolean;
  pathRef?: (el: SVGPathElement | null) => void;
  onSelect: (id: string) => void;
  onHover: (id: string | null) => void;
}

export function FlowEdge({
  edge,
  d,
  labelPos,
  color,
  strokeWidth,
  dimmed,
  selected,
  hovered,
  pathRef,
  onSelect,
  onHover,
}: FlowEdgeProps) {
  const running = edge.status === "running";
  const showLabel = edge.kind === "tool";

  return (
    <g
      className={[
        "fedge",
        selected ? "fedge--selected" : "",
        hovered ? "fedge--hovered" : "",
        edge.status === "error" ? "fedge--error" : "",
        running ? "fedge--running" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      opacity={dimmed ? 0.18 : 1}
    >
      {/* Filigrane du tracé (anti-aliasing façon noeud/aiguille) */}
      <path d={d} fill="none" stroke={color} strokeOpacity={0.16} strokeWidth={strokeWidth + 3} />

      {/* Tracé principal */}
      <path
        ref={pathRef}
        d={d}
        fill="none"
        stroke={color}
        strokeWidth={selected ? strokeWidth + 2 : strokeWidth}
        strokeLinecap="round"
        strokeDasharray={running ? "6 5" : "none"}
        className="fedge__path"
        onMouseEnter={() => onHover(edge.id)}
        onMouseLeave={() => onHover(null)}
      />

      {/* Lueur répétée pendant l'exécution (outil en cours) */}
      {running && (
        <path
          d={d}
          fill="none"
          stroke={color}
          strokeWidth={strokeWidth + 2}
          strokeOpacity={0.55}
          strokeLinecap="round"
          className="fedge__flow"
        />
      )}

      {/* Étiquette = nom de l'outil */}
      {showLabel && (
        <g
          transform={`translate(${labelPos.x}, ${labelPos.y})`}
          className="fedge__label"
          onClick={(e) => {
            e.stopPropagation();
            onSelect(edge.id);
          }}
        >
          <rect
            x={-Math.max(10, (edge.label.length * 6) / 2 + 8)}
            y={-8}
            width={Math.max(20, edge.label.length * 6 + 16)}
            height={16}
            rx={8}
            fill="rgba(11, 14, 17, 0.92)"
            stroke={color}
            strokeOpacity={0.5}
            strokeWidth={0.75}
          />
          <text
            textAnchor="middle"
            dominantBaseline="central"
            fontSize={9.5}
            fill={color}
            fontFamily="IBM Plex Mono, monospace"
          >
            {edge.label}
          </text>
        </g>
      )}

      {/* Info (tooltip natif : arguments + résumé) */}
      <title>
        {edge.kind === "tool"
          ? `${edge.tool ?? edge.label}${edge.args ? `\nargs: ${edge.args}` : ""}${
              edge.summary ? `\n→ ${edge.summary}` : ""
            }${Number.isFinite(edge.totalDurationMs) ? `\n· ${formatMs(edge.totalDurationMs / edge.count)}` : ""}`
          : `${edge.label} · ${edge.count} occurrence${edge.count > 1 ? "s" : ""}`}
      </title>
    </g>
  );
}

function formatMs(ms: number): string {
  if (!Number.isFinite(ms)) return "—";
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms)} ms`;
}