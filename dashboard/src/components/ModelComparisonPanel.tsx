/**
 * ModelComparisonPanel.tsx — Panneau premium de comparaison v1 vs v2.
 */

import { useEffect, useState } from "react";
import type { SentimentApiClient } from "../api/sentimentApiClient";
import type { ConfusionData } from "./types";

const V1_COLOR = "#5b8def";
const V2_COLOR = "#a855f7";

const METRIC_KEYS = [
  { key: "accuracy", label: "Accuracy" },
  { key: "f1_macro", label: "F1 macro" },
] as const;

const CLASS_ORDER = ["negative", "neutral", "positive"];

interface EvalState {
  status: "idle" | "loading" | "done" | "error";
  data: ConfusionData | null;
  error: string | null;
  forModel: string | null;
}

const COL_LABEL: Record<string, string> = { negative: "Négatif", neutral: "Neutre", positive: "Positif" };

interface MetricRowData {
  key: string;
  label: string;
  v1: number | null;
  v2: number | null;
}

function useEval(client: SentimentApiClient, model?: string): EvalState {
  const [result, setResult] = useState<EvalState>({ status: "idle", data: null, error: null, forModel: null });
  const currentKey = model || null;

  useEffect(() => {
    let cancelled = false;
    client
      .getConfusion({ model: model || undefined })
      .then((data) => {
        if (!cancelled) setResult({ status: "done", data: data as ConfusionData | null, error: null, forModel: model || null });
      })
      .catch((err: Error) => {
        if (!cancelled)
          setResult({ status: "error", data: null, error: err?.message || String(err), forModel: model || null });
      });
    return () => { cancelled = true; };
  }, [client, model]);

  if (result.forModel !== currentKey) {
    return { status: "loading", data: null, error: null, forModel: currentKey };
  }
  return result;
}

function buildRows(evalA: ConfusionData | null, evalB: ConfusionData | null): MetricRowData[] {
  const rows: MetricRowData[] = [];
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

function ValueBadge({ value, color }: { value: number | null; color: string }) {
  if (value == null) return <span className="tt-badge tt-badge-na">—</span>;
  return (
    <span className="tt-badge" style={{ borderColor: color, color }}>
      {Number((value * 100).toFixed(1))}%
    </span>
  );
}

function DiffBadge({ a, b }: { a: number | null; b: number | null }) {
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

function MetricRow({ label, v1, v2 }: { label: string; v1: number | null; v2: number | null }) {
  const pct = (v: number | null) => (v == null ? 0 : Math.min(100, Math.max(0, v * 100)));
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

function SideState({ state, color, name }: { state: EvalState | null; color: string; name: string }) {
  if (state?.status === "loading")
    return <span className="tt-cmp-side tt-cmp-side-loading">{name} : calcul…</span>;
  if (state?.status === "error")
    return (
      <span className="tt-cmp-side tt-cmp-side-error" title={state.error ?? undefined}>
        {name} : indisponible
      </span>
    );
  return (
    <span className="tt-cmp-side" style={{ color }}>
      <span className="tt-cmp-side-dot" style={{ background: color }} />
      {name}
    </span>
  );
}

export default function ModelComparisonPanel({
  client,
  modelA,
  modelB,
}: {
  client: SentimentApiClient;
  modelA?: string;
  modelB?: string;
}) {
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
        (evalA?.status === "error" || evalB.status === "error") && (
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
          <MetricRow key={row.key} label={row.label} v1={row.v1} v2={row.v2} />
        ))}
      </div>
    </div>
  );
}
