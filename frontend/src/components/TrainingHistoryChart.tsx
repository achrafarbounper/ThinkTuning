/**
 * TrainingHistoryChart.tsx — Graphique de l'historique d'entraînement.
 */

import { useEffect, useState } from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { SentimentApiClient } from "../api/sentimentApiClient";

interface HistoryPoint {
  job_id?: string;
  step?: number;
  loss?: number;
  val_loss?: number;
  epoch?: number;
  accuracy?: number;
  val_accuracy?: number;
  f1_macro?: number;
  val_f1_macro?: number;
  [key: string]: unknown;
}

interface ChartDataPoint {
  step: number;
  loss: number | null;
  val_loss: number | null;
  accuracy: number | null;
  val_accuracy: number | null;
  f1_macro: number | null;
  val_f1_macro: number | null;
}

export default function TrainingHistoryChart({
  jobs,
  client,
  pushLog,
}: {
  jobs: Array<{ job_id: string; status: string }>;
  client: SentimentApiClient;
  pushLog?: (level: string, message: string) => void;
}) {
  const [history, setHistory] = useState<HistoryPoint[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const completedJobs = jobs.filter((j) => j.status === "completed");
    if (completedJobs.length === 0) {
      setHistory([]);
      return undefined;
    }

    const loadHistory = async () => {
      setLoading(true);
      setError(null);
      try {
        const allHistory: HistoryPoint[] = [];
        for (const job of completedJobs) {
          try {
            const data = (await client.getTrainingHistory(job.job_id)) as HistoryPoint[];
            if (Array.isArray(data)) {
              allHistory.push(...data);
            }
          } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : String(err);
            pushLog?.("error", "Erreur historique " + job.job_id.slice(0, 8) + " : " + msg);
          }
        }
        if (!cancelled) {
          setHistory(allHistory);
        }
      } catch (err: unknown) {
        if (!cancelled) {
          const msg = err instanceof Error ? err.message : String(err);
          setError(msg);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    loadHistory();
    return () => { cancelled = true; };
  }, [jobs, client, pushLog]);

  const chartData: ChartDataPoint[] = history.map((point, index) => ({
    step: point.step ?? index,
    loss: point.loss ?? null,
    val_loss: point.val_loss ?? null,
    accuracy: point.accuracy ?? null,
    val_accuracy: point.val_accuracy ?? null,
    f1_macro: point.f1_macro ?? null,
    val_f1_macro: point.val_f1_macro ?? null,
  }));

  if (loading) {
    return <p className="tt-hint">Chargement de l'historique…</p>;
  }

  if (error) {
    return <p className="tt-hint tt-hint-error">Erreur : {error}</p>;
  }

  if (chartData.length === 0) {
    return <p className="tt-hint">Aucun historique d'entraînement disponible.</p>;
  }

  return (
    <div className="tt-chart-container">
      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={chartData} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e242c" />
          <XAxis
            dataKey="step"
            stroke="#8b94a3"
            tick={{ fill: "#8b94a3", fontSize: 12 }}
          />
          <YAxis
            stroke="#8b94a3"
            tick={{ fill: "#8b94a3", fontSize: 12 }}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: "#1a1f26",
              border: "1px solid #2a2f37",
              borderRadius: 8,
              color: "#e5e7eb",
            }}
          />
          <Legend />
          <Line
            type="monotone"
            dataKey="loss"
            stroke="#ef4444"
            strokeWidth={2}
            dot={false}
            name="Loss (train)"
            connectNulls
          />
          <Line
            type="monotone"
            dataKey="val_loss"
            stroke="#f97316"
            strokeWidth={2}
            dot={false}
            name="Loss (val)"
            connectNulls
          />
          <Line
            type="monotone"
            dataKey="accuracy"
            stroke="#22c55e"
            strokeWidth={2}
            dot={false}
            name="Accuracy (train)"
            connectNulls
          />
          <Line
            type="monotone"
            dataKey="val_accuracy"
            stroke="#3b82f6"
            strokeWidth={2}
            dot={false}
            name="Accuracy (val)"
            connectNulls
          />
          <Line
            type="monotone"
            dataKey="f1_macro"
            stroke="#a855f7"
            strokeWidth={2}
            dot={false}
            name="F1 macro (train)"
            connectNulls
          />
          <Line
            type="monotone"
            dataKey="val_f1_macro"
            stroke="#ec4899"
            strokeWidth={2}
            dot={false}
            name="F1 macro (val)"
            connectNulls
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
