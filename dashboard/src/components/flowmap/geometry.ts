/**
 * geometry.ts — Disposition (layout) et géométrie des arcs du Flow Map.
 *
 * Le planificateur est placé en haut au centre ; les agents (workers) sont
 * étalés sur une ligne en dessous. Chaque arc est une courbe quadratique
 * verticale entre le bas du planificateur et le haut de l'agent (et l'inverse
 * pour les retours), ce qui rend les impulsions faciles à animer.
 */

import type { AgentNodeData } from "./types";

/**
 * Dimensions de la toile (UX : lisibilité d'abord) — nœuds généreux pour un
 * balayage rapide du graphe, espacements aérés pour laisser respirer les arcs
 * et leurs étiquettes.
 */
export const NODE_W = 220;
export const NODE_H = 80;
const ROW_GAP = 180;
const NODE_GAP = 76;

export interface Position {
  x: number;
  y: number;
}

export interface Layout {
  positions: Record<string, Position>;
  width: number;
  height: number;
}

/**
 * Calcule la position des nœuds. `nodeOrder` conserve l'ordre d'apparition ;
 * le planificateur (role:planner) reste au sommet, les autres n'étant dessinés
 * que s'ils existent dans `nodes`/`order`.
 */
export function computeLayout(
  nodes: AgentNodeData[],
  nodeOrder: string[],
): Layout {
  const workers = nodeOrder
    .map((id) => nodes.find((n) => n.id === id))
    .filter((n): n is AgentNodeData => Boolean(n))
    .filter((n) => n.id !== "role:planner");

  const positions: Record<string, Position> = {};

  // Planificateur au centre (x=0), en haut.
  positions["role:planner"] = { x: 0, y: 0 };

  const count = Math.max(1, workers.length);
  const widthNeeded = count * NODE_W + (count - 1) * NODE_GAP;
  let x = -widthNeeded / 2 + NODE_W / 2;
  for (const node of workers) {
    positions[node.id] = { x, y: ROW_GAP };
    x += NODE_W + NODE_GAP;
  }

  const height = ROW_GAP + NODE_H;
  return { positions, width: Math.max(widthNeeded, NODE_W), height };
}

/** Centre-bas du nœud (point de départ des arcs sortants du planificateur). */
function bottomCenter(pos: Position): [number, number] {
  return [pos.x, pos.y + NODE_H / 2];
}

/** Centre-haut du nœud (point d'arrivée des arcs entrants vers l'agent). */
function topCenter(pos: Position): [number, number] {
  return [pos.x, pos.y - NODE_H / 2];
}

/** Crochet vertical : différence de y entre départ et arrivée, bornée. */
function bend(sy: number, ty: number): number {
  const d = Math.max(28, Math.abs(sy - ty) * 0.5);
  return sy < ty ? d : -d;
}

interface Pt {
  x: number;
  y: number;
}

/**
 * Points de contrôle de la courbe quadratique d'un arc. `incoming` = vrai quand
 * l'arc part d'un agent vers le planificateur (retour), sinon planner → agent.
 * Un seul calcul partagé par le tracé (chemin) et le marqueur de direction,
 * garantit que la flèche reste toujours posée « sur » la courbe.
 */
function controlPoints(
  source: Position,
  target: Position,
  incoming: boolean,
): [Pt, Pt, Pt] {
  const [sx, sy] = incoming ? topCenter(source) : bottomCenter(source);
  const [tx, ty] = incoming ? bottomCenter(target) : topCenter(target);
  const c = incoming ? sy - bend(sy, ty) : sy + bend(sy, ty);
  return [
    { x: sx, y: sy },
    { x: sx, y: c },
    { x: tx, y: ty },
  ];
}

/**
 * Chemin (attribut `d`) d'un arc entre deux positions. `incoming` = vrai quand
 * l'arc part d'un agent vers le planificateur (retour), sinon planner → agent.
 */
export function edgePath(source: Position, target: Position): string {
  return curvePath(controlPoints(source, target, false));
}

/** Version « retour » : part de l'agent (haut) vers le planificateur (bas). */
export function returnPath(source: Position, target: Position): string {
  return curvePath(controlPoints(source, target, true));
}

function curvePath([p0, p1, p2]: [Pt, Pt, Pt]): string {
  return `M ${p0.x} ${p0.y} C ${p1.x} ${p1.y}, ${p2.x} ${p2.y}, ${p2.x} ${p2.y}`;
}

export interface ArrowGeometry {
  x: number;
  y: number;
  angle: number;
}

/**
 * Position + orientation d'une flèche de direction placée sur la courbe :
 * point à l'abscisse `t` et tangente (dérivée de la quadratique). Rend le sens
 * du flux (planner → worker, retour worker → planner) lisible d'un coup d'œil.
 */
export function arrowGeometry(
  source: Position,
  target: Position,
  incoming: boolean,
  t = 0.58,
): ArrowGeometry {
  const [p0, p1, p2] = controlPoints(source, target, incoming);
  const mt = 1 - t;
  return {
    x: mt * mt * p0.x + 2 * mt * t * p1.x + t * t * p2.x,
    y: mt * mt * p0.y + 2 * mt * t * p1.y + t * t * p2.y,
    angle:
      (Math.atan2(
        2 * mt * (p1.y - p0.y) + 2 * t * (p2.y - p1.y),
        2 * mt * (p1.x - p0.x) + 2 * t * (p2.x - p1.x),
      ) *
        180) /
      Math.PI,
  };
}

/** Bornes (min/max x,y) de tous les centres, pour cadrer la vue (fit-view). */
export function computeBounds(positions: Record<string, Position>): {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
} {
  const values = Object.values(positions);
  if (values.length === 0) {
    return { minX: 0, minY: 0, maxX: NODE_W, maxY: NODE_H };
  }
  const pad = 150;
  return {
    minX: Math.min(...values.map((p) => p.x - NODE_W / 2)) - pad,
    minY: Math.min(...values.map((p) => p.y - NODE_H)) - pad,
    maxX: Math.max(...values.map((p) => p.x + NODE_W / 2)) + pad,
    maxY: Math.max(...values.map((p) => p.y + NODE_H)) + pad,
  };
}