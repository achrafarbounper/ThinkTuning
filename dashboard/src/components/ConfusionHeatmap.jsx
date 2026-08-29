/**
 * ConfusionHeatmap.jsx
 * ---------------------------------------------------------------------
 * Heatmap interactive de la matrice de confusion, rendue avec recharts.
 *
 * Reçoit la réponse de `GET /evaluate/confusion` :
 *   { labels: ["negative","neutral","positive"],
 *     matrix: [[3x3]], metrics, errors_by_class, n, model }
 *
 * Ligne = vrai, colonne = prédit. La diagonale (bonnes prédictions) est en
 * vert, les cellules hors-diagonale (erreurs) en rouge. Le survol affiche
 * « Vrai = X · Prédit = Y · Nombre = N ».
 */

import { useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  ScatterChart,
  Scatter,
  XAxis,
  YAxis,
  ZAxis,
  Tooltip,
} from "recharts";

const HEIGHT = 320;
const AXIS_MARGIN = { top: 10, right: 20, bottom: 46, left: 80 };

/** Couleur d'une cellule selon qu'elle est sur la diagonale (correcte). */
function cellFill(value, max, isCorrect) {
  const t = max > 0 ? value / max : 0;
  if (isCorrect) return `rgba(47, 191, 113, ${0.22 + 0.78 * t})`; // vert
  return `rgba(242, 84, 91, ${0.18 + 0.82 * t})`; // rouge (erreur)
}

/** Cellule custom : un rect + le compteur, centré sur les coordonnées recharts. */
function CellRect({ cx, cy, cellW, cellH, value, max, isCorrect }) {
  if (cx == null || cy == null) return null;
  const w = Math.max(8, cellW * 0.92);
  const h = Math.max(8, cellH * 0.92);
  const fill = cellFill(value, max, isCorrect);
  return (
    <g>
      <rect
        x={cx - w / 2}
        y={cy - h / 2}
        width={w}
        height={h}
        rx={6}
        fill={fill}
        stroke="rgba(255,255,255,0.08)"
      />
      <text
        x={cx}
        y={cy + 4}
        textAnchor="middle"
        fontSize={Math.min(16, cellW * 0.32)}
        fontWeight={600}
        fill={value > 0 ? "#0b0e11" : "rgba(139,148,163,0.6)"}
      >
        {value}
      </text>
    </g>
  );
}

/** Tooltip maison : infos lisibles plutôt que les coordonnées brutes. */
function ConfusionTooltip({ active, payload }) {
  if (!active || !payload || !payload.length) return null;
  const p = payload[0]?.payload;
  if (!p) return null;
  return (
    <div className="tt-heat-tooltip">
      <strong>{p.value} exemple(s)</strong>
      <span>Vrai : {p.trueLabel}</span>
      <span>Prédit : {p.predLabel}</span>
      <span className={p.isCorrect ? "tt-heat-ok" : "tt-heat-err"}>
        {p.isCorrect ? "✓ bonne prédiction" : "✗ erreur"}
      </span>
    </div>
  );
}

export default function ConfusionHeatmap({ data }) {
  const wrapRef = useRef(null);
  const [width, setWidth] = useState(0);

  // Mesure la largeur du conteneur pour dimensionner parfaitement les cellules.
  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return undefined;
    const measure = () => setWidth(el.clientWidth);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const innerW = Math.max(120, width - AXIS_MARGIN.left - AXIS_MARGIN.right);
  const innerH = HEIGHT - AXIS_MARGIN.top - AXIS_MARGIN.bottom;
  const cellW = innerW / 3;
  const cellH = innerH / 3;

  const cells = useMemo(() => {
    const labels = data?.labels || [];
    const matrix = data?.matrix || [];
    const max = matrix.flat().reduce((m, v) => (v > m ? v : m), 0);
    const out = [];
    matrix.forEach((row, trueIdx) => {
      row.forEach((value, predIdx) => {
        out.push({
          x: predIdx + 0.5, // cellule i → centre du band i (domaine [0,3])
          y: trueIdx + 0.5,
          value,
          max,
          trueIdx,
          predIdx,
          trueLabel: labels[trueIdx],
          predLabel: labels[predIdx],
          isCorrect: trueIdx === predIdx,
        });
      });
    });
    return out;
  }, [data]);

  if (!data || !data.matrix || !data.matrix.length) {
    return <p className="tt-hint">Aucune donnée de matrice de confusion.</p>;
  }

  return (
    <div className="tt-heat" ref={wrapRef}>
      {width > 0 && (
        <ScatterChart width={width} height={HEIGHT} margin={AXIS_MARGIN}>
          <ZAxis type="number" range={[100, 100]} />
          <XAxis
            type="number"
            dataKey="x"
            domain={[0, 3]}
            ticks={[0.5, 1.5, 2.5]}
            allowDataOverflow
            tickFormatter={(t) => data.labels[Math.round(t - 0.5)]}
            tick={{ fill: "#8b94a3", fontSize: 12 }}
            tickLine={false}
            axisLine={{ stroke: "#1e242c" }}
            label={{ value: "Prédit", position: "insideBottom", offset: -16, fill: "#8b94a3", fontSize: 12 }}
            height={46}
          />
          <YAxis
            type="number"
            dataKey="y"
            domain={[0, 3]}
            ticks={[0.5, 1.5, 2.5]}
            allowDataOverflow
            reversed
            tickFormatter={(t) => data.labels[Math.round(t - 0.5)]}
            tick={{ fill: "#8b94a3", fontSize: 12 }}
            tickLine={false}
            axisLine={{ stroke: "#1e242c" }}
            label={{ value: "Vrai", angle: -90, position: "insideLeft", offset: 8, fill: "#8b94a3", fontSize: 12 }}
            width={80}
          />
          <Tooltip content={<ConfusionTooltip />} cursor={{ fill: "rgba(255,255,255,0.03)" }} />
          <Scatter
            data={cells}
            isAnimationActive={false}
            shape={(props) => <CellRect {...props} cellW={cellW} cellH={cellH} />}
          />
        </ScatterChart>
      )}
    </div>
  );
}
