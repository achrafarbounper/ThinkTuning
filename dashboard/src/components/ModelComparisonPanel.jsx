/**
 * ModelComparisonPanel.jsx
 * ---------------------------------------------------------------------
 * Panneau premium de comparaison v1 vs v2, au style « dashboard ML haut de
 * gamme » (Vercel / Linear / W&B) : fond flou translucide, coins arrondis,
 * ombre douce, hover glass.
 *
 * Pour deux versions de modèles choisies, il affiche les mêmes métriques
 * calculées côté serveur (GET /evaluate/confusion) :
 *   - Accuracy
 *   - F1 macro
 *   - Rappel par classe (per_class_recall)
 * avec barres fines + rond terminal (v1 = bleu, v2 = violet), valeurs en
 * badges arrondis et badge de différence v2 – v1 à droite.
 */

import { useEffect, useState } from "react";

const V1_COLOR = "#5b8def"; // bleu
const V2_COLOR = "#a855f7"; // violet

const METRIC_KEYS = [
  { key: "accuracy", label: "Accuracy" },
  { key: "f1_macro", label: "F1 macro" },
];

const CLASS_ORDER = ["negative", "neutral", "positive"];

function useEval(client, model) {
  const [result, setResult] = useState({ status: "idle", data: null, error: null, forModel: null });

  const currentKey = model || null;

  useEffect(() => {
    let cancelled = false;
    client
      .getConfusion({ model: model || null })
      .then((data) => {
        if (!cancelled) setResult({ status: "done", data, error: null, forModel: model || null });
      })
      .catch((err) => {
        if (!cancelled)
          setResult({
            status: "error",
            data: null,
            error: err?.message || String(err),
            forModel: model || null,
          });
      });
    return () => {
      cancelled = true;
    };
  }, [client, model]);

  // « loading » dérivé tant que la donnée en cache ne correspond pas au modèle courant.
  if (result.forModel !== currentKey) {
    return { status: "loading", data: null, error: null };
  }
  return result;
}

const COL_LABEL = { negative: "Négatif", neutral: "Neutre", positive: "Positif" };

function buildRows(evalA, evalB) {
  const rows = [];

  for (const { key, label } of METRIC_KEYS) {
    rows.push({ key, label, v1: evalA?.metrics?.[key] ?? null, v2: evalB?.metrics?.[key] ?? null });
  }

  for (const cls of CLASS_ORDER) {
    rows.push({
      key: `recall_${cls}`,
      label: `Rappel · ${COL_LABEL[cls] || cls}`,
      v1: evalA?.metrics?.per_class_recall?.[cls] ?? null,
      v2: evalB?.metrics?.per_class_recall?.[cls] ?? null,
    });
  }

  return rows;
}

function ValueBadge({ value, color }) {
  if (value == null) return <span className="tt-badge tt-badge-na">—</span>;
  return (
    <span className="tt-badge" style={{ borderColor: color, color }}>
      {Number((value * 100).toFixed(1))}%
    </span>
  );
}

function DiffBadge({ a, b }) {
  if (a == null || b == null) return <span className="tt-badge tt-badge-na">n/a</span>;
  const diff = (b - a) * 100;
  const sign = diff > 0 ? "+" : diff < 0 ? "−" : "±";
  const cls = diff > 0 ? "tt-badge-up" : diff < 0 ? "tt-badge-down" : "tt-badge-flat";
  return (
    <span className={`tt-badge tt-badge-diff ${cls}`} title="Différence v2 – v1">
      {sign}
      {Math.abs(diff).toFixed(1)}%
    </span>
  );
}

function MetricRow({ label, v1, v2 }) {
  const pct = (v) => (v == null ? 0 : Math.min(100, Math.max(0, v * 100)));

  return (
    <div className="tt-cmp-row">
      <span className="tt-cmp-label">{label}</span>

      <div className="tt-cmp-bars">
        <span className="tt-cmp-bar tt-cmp-v1" title={`v1 : ${pct(v1).toFixed(1)}%`}>
          <span className="tt-cmp-fill" style={{ width: `${pct(v1)}%` }} />
          <span className="tt-cmp-dot" style={{ left: `${pct(v1)}%`, background: V1_COLOR }} />
        </span>
        <span className="tt-cmp-bar tt-cmp-v2" title={`v2 : ${pct(v2).toFixed(1)}%`}>
          <span className="tt-cmp-fill" style={{ width: `${pct(v2)}%` }} />
          <span className="tt-cmp-dot" style={{ left: `${pct(v2)}%`, background: V2_COLOR }} />
        </span>
      </div>

      <div className="tt-cmp-values">
        <ValueBadge value={v1} color={V1_COLOR} />
        <ValueBadge value={v2} color={V2_COLOR} />
        <DiffBadge a={v1} b={v2} />
      </div>
    </div>
  );
}

function SideState({ state, color, name }) {
  if (state?.status === "loading")
    return <span className="tt-cmp-side tt-cmp-side-loading">{name} : calcul…</span>;
  if (state?.status === "error")
    return (
      <span className="tt-cmp-side tt-cmp-side-error" title={state.error}>
        {name} : indisponible
      </span>
    );
  const statusFont = { color };
  return (
    <span className="tt-cmp-side" style={statusFont}>
      <span className="tt-cmp-side-dot" style={{ background: color }} />
      {name}
    </span>
  );
}

export default function ModelComparisonPanel({ client, modelA, modelB }) {
  const evalA = useEval(client, modelA);
  const evalB = useEval(client, modelB);

  const readyA = evalA?.status === "done";
  const readyB = evalB?.status === "done";
  const rows = buildRows(readyA ? evalA.data : null, readyB ? evalB.data : null);

  return (
    <div className="tt-cmp">
      <div className="tt-cmp-sides">
        <SideState state={evalA} color={V1_COLOR} name="v1" />
        <span className="tt-cmp-title">Comparaison de modèles</span>
        <SideState state={evalB} color={V2_COLOR} name="v2" />
      </div>

      {(!evalA?.status || (evalA.status !== "done" && evalB.status !== "done")) &&
        (evalA?.status === "error" || evalB?.status === "error") && (
          <p className="tt-hint tt-hint-error">
            {evalA?.error && `v1 : ${evalA.error} `}
            {evalB?.error && `v2 : ${evalB.error}`}
          </p>
        )}

      <div className="tt-cmp-head">
        <span>Métrique</span>
        <span className="tt-cmp-head-bars">Répartition</span>
        <span className="tt-cmp-head-values">
          <span style={{ color: V1_COLOR }}>v1</span>
          <span style={{ color: V2_COLOR }}>v2</span>
          <span>Δ v2−v1</span>
        </span>
      </div>

      <div className="tt-cmp-rows">
        {rows.map((row) => (
          <MetricRow key={row.key} {...row} />
        ))}
      </div>
    </div>
  );
}
