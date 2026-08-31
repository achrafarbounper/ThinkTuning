/**
 * Page « Monitoring » — métriques Prometheus en direct.
 * ---------------------------------------------------------------------
 * Interroge `GET /metrics` (format texte Prometheus) et, en repli,
 * `GET /metrics/json` (proxy JSON), puis affiche des graphiques recharts :
 *   - requêtes/min sur les 5 dernières minutes,
 *   - latence moyenne de `/predict`,
 *   - distribution des statuts HTTP.
 *
 * Sans Prometheus externe : les compteurs cumulés sont dérivés en débits par
 * différence entre deux scrutations (polling auto, par défaut 15 s).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useApp } from "../context/useApp";
import {
  parsePrometheusText,
  normalizeJsonProxy,
  aggregateSnapshot,
  deltaSnapshots,
} from "../api/prometheusParser";

const WINDOW_MS = 5 * 60 * 1000; // 5 dernières minutes
const STORAGE_KEY = "thinktuning.monitoringInterval";
const INTERVAL_OPTIONS = [
  { value: 5, label: "5 s" },
  { value: 10, label: "10 s" },
  { value: 15, label: "15 s" },
  { value: 30, label: "30 s" },
  { value: 60, label: "60 s" },
];

function loadInterval() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const n = parseInt(raw, 10);
    if (INTERVAL_OPTIONS.some((o) => o.value === n)) return n;
  } catch {
    /* stockage indisponible */
  }
  return 15;
}

const AXIS = { stroke: "none", tick: { fill: "#8b94a3", fontSize: 11 }, tickLine: false, axisLine: false };
const GRID = { stroke: "#1e242c", strokeDasharray: "3 3" };
const TOOLTIP_STYLE = { background: "#12161c", border: "1px solid #1e242c", borderRadius: 8, fontSize: 12 };

function timeLabel(ts) {
  return new Date(ts).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** Couleur d'un statut HTTP (harmonisée avec le thème). */
function statusColor(status) {
  const code = Number(status) || 0;
  if (code >= 500) return "#f2545b";
  if (code >= 400) return "#f2b705";
  if (code >= 300) return "#5b8def";
  return "#2fbf71";
}

/** Tooltip recharts sur mesure pour les valeurs numériques. */
function MetricTooltip({ active, payload, label, formatter, suffix }) {
  if (!active || !payload || !payload.length) return null;
  return (
    <div className="tt-mon-tooltip" style={TOOLTIP_STYLE}>
      <strong className="tt-mon-tooltip-time">{label}</strong>
      {payload.map((entry) => {
        const labelKey = formatter ? formatter(entry.dataKey) : entry.dataKey;
        return (
          <span key={String(entry.dataKey)}>
            {labelKey} : {entry.value != null ? Number(entry.value).toFixed(1) : 0}
            {suffix || ""}
          </span>
        );
      })}
    </div>
  );
}

function RequestsTooltip(props) {
  return <MetricTooltip {...props} formatter={() => "requêtes/min"} suffix="" />;
}

function LatencyTooltip(props) {
  return <MetricTooltip {...props} formatter={() => "latence"} suffix=" ms" />;
}

function StatusTooltip({ active, payload }) {
  if (!active || !payload || !payload.length) return null;
  const item = payload[0].payload;
  return (
    <div className="tt-mon-tooltip" style={TOOLTIP_STYLE}>
      <strong>Statut {item.status}</strong>
      <span>requêtes : {item.count}</span>
    </div>
  );
}

export default function MonitoringPage() {
  const { client } = useApp();
  const [intervalSec, setIntervalSec] = useState(loadInterval);
  const [paused, setPaused] = useState(false);
  const [error, setError] = useState(null);
  const [source, setSource] = useState(null); // "texte" | "json"
  const [lastScrapedAt, setLastScrapedAt] = useState(null);
  const [history, setHistory] = useState([]); // points fenêtre mobile 5 min
  const prevRef = useRef(null);

  const persistInterval = (sec) => {
    setIntervalSec(sec);
    try {
      window.localStorage.setItem(STORAGE_KEY, String(sec));
    } catch {
      /* stockage indisponible */
    }
  };

  const togglePaused = () => {
    const next = !paused;
    if (!next) prevRef.current = null; // re-baseline au retour (delta propre)
    setPaused(next);
  };
useEffect(() => {
    if (paused) return undefined;

    let cancelled = false;
    const scrape = async () => {
      let raw;
      let src = "texte";
      try {
        try {
          const text = await client.getMetricsRaw();
          raw = parsePrometheusText(text);
          if (!raw.counters.length && !raw.histograms.length) throw new Error("parse vide");
        } catch {
          raw = normalizeJsonProxy(await client.getMetricsJson());
          src = "json";
        }
      } catch (err) {
        if (!cancelled) setError(err && err.message ? err.message : String(err));
        return;
      }

      if (cancelled) return;
      const agg = aggregateSnapshot(raw);
      const delta = prevRef.current ? deltaSnapshots(prevRef.current, agg) : null;

      prevRef.current = agg;
      setSource(src);
      setLastScrapedAt(agg.scrapedAtMs);
      setError(null);

      if (delta) {
        setHistory((h) => {
          const point = {
            t: agg.scrapedAtMs,
            requestsPerMin: delta.requestsPerMin,
            requestDelta: delta.requestsTotal,
            predictLatencyMs: delta.latencyPredict && delta.latencyPredict.count > 0 ? delta.latencyPredict.meanMs : null,
            statuses: delta.requestsByStatus,
            byPath: delta.requestsByPath,
            latencyByPath: delta.latencyByPath,
          };
          const minT = agg.scrapedAtMs - WINDOW_MS;
          return [...h, point].filter((p) => p.t >= minT);
        });
      }
    };

    scrape();
    const id = setInterval(scrape, intervalSec * 1000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [client, intervalSec, paused]);

  // Agrégats sur la fenêtre mobile (5 min) pour les cartes + distribution.
  const windowSummary = useMemo(() => {
    const statuses = {};
    const byPath = {};
    const latencyByPath = {};
    let errors = 0;
    let total = 0;

    for (const p of history) {
      for (const s of p.statuses || []) {
        statuses[s.status] = (statuses[s.status] || 0) + s.delta;
        total += s.delta;
        if (Number(s.status) >= 500) errors += s.delta;
      }
      for (const r of p.byPath || []) {
        const key = `${r.method} ${r.path}`;
        const e = (byPath[key] = byPath[key] || { method: r.method, path: r.path, count: 0 });
        e.count += r.delta;
      }
      for (const l of p.latencyByPath || []) {
        const key = `${l.method} ${l.path}`;
        const e = (latencyByPath[key] = latencyByPath[key] || { method: l.method, path: l.path, count: 0, sum: 0 });
        e.count += l.count;
        e.sum += l.sum;
      }
    }

    const predictLatency = latencyByPath["POST /predict"];
    return {
      statuses: Object.entries(statuses)
        .sort(([a], [b]) => (Number(a) || 0) - (Number(b) || 0))
        .map(([status, count]) => ({ status, count })),
      byPath: Object.values(byPath).map((e) => ({
        ...e,
        meanMs: e.count ? ((latencyByPath[`${e.method} ${e.path}`]?.sum || 0) / e.count) * 1000 : 0,
      })),
      totalRequests: total,
      errorRate: total > 0 ? errors / total : 0,
      predict: predictLatency
        ? { count: predictLatency.count, meanMs: predictLatency.count ? (predictLatency.sum / predictLatency.count) * 1000 : 0 }
        : { count: 0, meanMs: 0 },
    };
  }, [history]);

  const latest = history.length ? history[history.length - 1] : null;
  const hasData = history.length > 0;
  const hasPredict = windowSummary.predict.count > 0;

  const lastTime = lastScrapedAt ? timeLabel(lastScrapedAt) : "—";
  const stats = [
    {
      label: "Requêtes / min",
      value: latest ? latest.requestsPerMin.toFixed(1) : "—",
      detail: `${windowSummary.totalRequests} requête(s) · 5 min`,
    },
    {
      label: "Latence /predict",
      value: hasPredict ? `${windowSummary.predict.meanMs.toFixed(0)} ms` : "—",
      detail: hasPredict ? `${windowSummary.predict.count} appel(s) observés` : "Aucun appel récent",
    },
    {
      label: "Taux d'erreur (5xx)",
      value: latest ? `${Math.round(windowSummary.errorRate * 100)}%` : "—",
      detail: `${windowSummary.statuses.filter((s) => Number(s.status) >= 500).reduce((a, b) => a + b.count, 0)} sur ${windowSummary.totalRequests}`,
    },
    {
      label: "Dernière scrutation",
      value: lastTime,
      detail: `Source : ${source === "json" ? "proxy JSON" : source === "texte" ? "texte /metrics" : "—"}`,
    },
  ];

  const handleReset = () => {
    prevRef.current = null;
    setHistory([]);
    setError(null);
  };
const chartData = history.map((p) => ({ ...p, t: timeLabel(p.t) }));

  return (
    <>
      <header className="page-head">
        <h1>Monitoring</h1>
        <p>
          Métriques Prometheus de l'API scrapées en direct depuis /metrics — sans Grafana ni Prometheus externe.
        </p>
      </header>

      <div className="page-body">
        {/* Contrôles de polling */}
        <section className="tt-mon-controls">
          <label className="tt-mon-control">
            <span>Intervalle de scrutation</span>
            <select value={intervalSec} onChange={(e) => persistInterval(Number(e.target.value))}>
              {INTERVAL_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>

          <button
            type="button"
            className={`tt-btn ${paused ? "tt-btn-primary" : "tt-btn-ghost"}`}
            onClick={togglePaused}
          >
            {paused ? "Reprendre" : "Pause"}
          </button>
          <button type="button" className="tt-btn tt-btn-ghost" onClick={handleReset}>
            Réinitialiser
          </button>

          <span className="tt-hint tt-mon-status">
            {paused ? "Scrutation en pause" : `Scrutation toutes les ${INTERVAL_OPTIONS.find((o) => o.value === intervalSec)?.label}`}
            {source && !paused && (
              <span className={source === "json" ? "tt-mon-src tt-mon-src-json" : "tt-mon-src"}>
                {" "}
                {source === "json" ? "proxy JSON" : "texte"}
              </span>
            )}
          </span>
        </section>

        {error && <p className="tt-hint tt-hint-error">Erreur de scrutation : {error}</p>}

        {/* Cartes statistiques */}
        <div className="home-stats">
          {stats.map((s) => (
            <section className="tt-panel home-stat" key={s.label}>
              <p className="home-stat__label">{s.label}</p>
              <p className="home-stat__value">{s.value}</p>
              <p className="home-stat__detail">{s.detail}</p>
            </section>
          ))}
        </div>

        {!hasData && !error && (
          <section className="tt-panel">
            <p className="tt-hint">
              Collecte des métriques… les graphiques se remplissent au fil des scrutations (historique 5 minutes).
            </p>
          </section>
        )}
{hasData && (
          <>
            {/* Requêtes / min (5 dernières minutes) */}
            <section className="tt-panel">
              <div className="tt-panel-head">
                <h2>Requêtes / minute</h2>
                <span className="tt-tag">5 dernières minutes</span>
              </div>
              <ResponsiveContainer width="100%" height={280}>
                <AreaChart data={chartData}>
                  <CartesianGrid {...GRID} />
                  <XAxis dataKey="t" {...AXIS} />
                  <YAxis {...AXIS} />
                  <Tooltip content={<RequestsTooltip />} />
                  <Area
                    type="monotone"
                    dataKey="requestsPerMin"
                    stroke="#5b8def"
                    fill="rgba(91,141,239,0.25)"
                    isAnimationActive={false}
                    strokeWidth={2}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </section>

            {/* Latence moyenne /predict */}
            <section className="tt-panel">
              <div className="tt-panel-head">
                <h2>Latence moyenne /predict</h2>
                <span className="tt-tag">millisecondes</span>
              </div>
              <ResponsiveContainer width="100%" height={280}>
                <LineChart data={chartData}>
                  <CartesianGrid {...GRID} />
                  <XAxis dataKey="t" {...AXIS} />
                  <YAxis {...AXIS} />
                  <Tooltip content={<LatencyTooltip />} />
                  <Line
                    type="monotone"
                    dataKey="predictLatencyMs"
                    stroke="#2fbf71"
                    dot={false}
                    isAnimationActive={false}
                    strokeWidth={2}
                  />
                </LineChart>
              </ResponsiveContainer>
            </section>

            {/* Distribution des statuts */}
            <section className="tt-panel">
              <div className="tt-panel-head">
                <h2>Distribution des statuts HTTP</h2>
                <span className="tt-tag">{windowSummary.totalRequests} requêtes</span>
              </div>
              <ResponsiveContainer width="100%" height={240}>
                <BarChart data={windowSummary.statuses}>
                  <CartesianGrid {...GRID} />
                  <XAxis dataKey="status" {...AXIS} />
                  <YAxis {...AXIS} allowDecimals={false} />
                  <Tooltip content={<StatusTooltip />} cursor={{ fill: "rgba(255,255,255,0.03)" }} />
                  <Bar dataKey="count" isAnimationActive={false}>
                    {windowSummary.statuses.map((s) => (
                      <Cell key={s.status} fill={statusColor(s.status)} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </section>
{/* Tableau par route */}
            {windowSummary.byPath.length > 0 && (
              <section className="tt-panel">
                <div className="tt-panel-head">
                  <h2>Activité par route</h2>
                  <span className="tt-tag">5 dernières minutes</span>
                </div>
                <table className="tt-table">
                  <thead>
                    <tr>
                      <th>Méthode</th>
                      <th>Route</th>
                      <th>Requêtes</th>
                      <th>Latence moyenne</th>
                    </tr>
                  </thead>
                  <tbody>
                    {windowSummary.byPath.map((r) => (
                      <tr key={`${r.method} ${r.path}`}>
                        <td>
                          <span
                            className={`tt-tag ${r.method === "GET" ? "tt-tag-status-completed" : "tt-tag-status-running"}`}
                          >
                            {r.method}
                          </span>
                        </td>
                        <td className="tt-mono">{r.path}</td>
                        <td>{r.count}</td>
                        <td className="tt-mono">{r.meanMs.toFixed(0)} ms</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
            )}
          </>
        )}
      </div>
    </>
  );
}