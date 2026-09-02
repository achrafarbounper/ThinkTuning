/**
 * TrainMetricsStream.tsx — Flux de métriques en temps réel pour un job.
 */

import { useEffect, useState } from "react";
import type { SentimentApiClient } from "../api/sentimentApiClient";

interface MetricPoint {
  timestamp?: number;
  step?: number;
  epoch?: number;
  loss?: number;
  accuracy?: number;
  f1_macro?: number;
  [key: string]: unknown;
}

export default function TrainMetricsStream({
  jobId,
  client,
}: {
  jobId: string;
  client: SentimentApiClient;
}) {
  const [latest, setLatest] = useState<MetricPoint | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let ws: WebSocket | null = null;

    try {
      const url = client.getTrainMetricsStreamUrl(jobId);
      ws = new WebSocket(url);

      ws.onmessage = (event) => {
        if (cancelled) return;
        try {
          const data = JSON.parse(event.data) as MetricPoint;
          setLatest(data);
          setLoading(false);
        } catch {
          // ignore malformed messages
        }
      };

      ws.onerror = () => {
        if (!cancelled) {
          setError("Erreur de connexion WebSocket");
          setLoading(false);
        }
      };

      ws.onopen = () => {
        if (!cancelled) setLoading(true);
      };
    } catch (err: unknown) {
      if (!cancelled) {
        const msg = err instanceof Error ? err.message : String(err);
        setError(msg);
        setLoading(false);
      }
    }

    return () => {
      cancelled = true;
      if (ws) ws.close();
    };
  }, [jobId, client]);

  if (loading && !latest) {
    return <p className="tt-hint">Chargement des métriques…</p>;
  }

  if (error) {
    return <p className="tt-hint tt-hint-error">Erreur : {error}</p>;
  }

  if (!latest) {
    return <p className="tt-hint">Aucune métrique disponible.</p>;
  }

  return (
    <div className="tt-metrics-stream">
      <div className="tt-metrics-grid">
        <div className="tt-metric-card">
          <span className="tt-metric-label">Epoch</span>
          <span className="tt-metric-value">{latest.epoch ?? "—"}</span>
        </div>
        <div className="tt-metric-card">
          <span className="tt-metric-label">Step</span>
          <span className="tt-metric-value">{latest.step ?? "—"}</span>
        </div>
        <div className="tt-metric-card">
          <span className="tt-metric-label">Loss</span>
          <span className="tt-metric-value">
            {latest.loss != null ? latest.loss.toFixed(4) : "—"}
          </span>
        </div>
        <div className="tt-metric-card">
          <span className="tt-metric-label">Accuracy</span>
          <span className="tt-metric-value">
            {latest.accuracy != null ? (latest.accuracy * 100).toFixed(1) + " %" : "—"}
          </span>
        </div>
        <div className="tt-metric-card">
          <span className="tt-metric-label">F1 macro</span>
          <span className="tt-metric-value">
            {latest.f1_macro != null ? (latest.f1_macro * 100).toFixed(1) + " %" : "—"}
          </span>
        </div>
      </div>
    </div>
  );
}
