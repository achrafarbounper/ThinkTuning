/**
 * StepLogConsole.jsx
 * ---------------------------------------------------------------------
 * Console de logs live par étape : affiche les événements "log" reçus du
 * WebSocket /train/stream/{job_id} (message, niveau, étape d'origine),
 * avec auto-scroll et filtre optionnel par étape.
 */
import { useEffect, useMemo, useRef, useState } from "react";

const LEVEL_CLASS = {
  ERROR: "tt-log-error",
  WARNING: "tt-log-warning",
  INFO: "tt-log-info",
  DEBUG: "tt-log-debug",
};

export default function StepLogConsole({ logs, stepLabels = {} }) {
  const [filter, setFilter] = useState("all"); // "all" | étape courante uniquement
  const listRef = useRef(null);

  const steps = useMemo(() => {
    const seen = [];
    for (const log of logs) {
      if (log.step && !seen.includes(log.step)) seen.push(log.step);
    }
    return seen;
  }, [logs]);

  const visible = useMemo(() => {
    if (filter === "all") return logs;
    return logs.filter((l) => l.step === filter);
  }, [logs, filter]);

  // Auto-scroll vers la dernière ligne (le flux est chronologique).
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [visible.length]);

  return (
    <div className="tt-log-console">
      <div className="tt-log-toolbar">
        <span className="tt-log-title">Logs serveur</span>
        <select
          className="tt-log-filter"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        >
          <option value="all">Toutes les étapes</option>
          {steps.map((s) => (
            <option key={s} value={s}>
              {stepLabels[s] || s}
            </option>
          ))}
        </select>
      </div>
      <div className="tt-log-list" ref={listRef}>
        {visible.length === 0 && (
          <p className="tt-log-empty">Aucun log pour le moment…</p>
        )}
        {visible.map((log) => (
          <div
            key={log.seq}
            className={`tt-log-line ${LEVEL_CLASS[log.level] || "tt-log-info"}`}
          >
            <span className="tt-log-time">
              {new Date((log.ts || 0) * 1000).toLocaleTimeString()}
            </span>
            {log.step && (
              <span className="tt-log-step">
                {stepLabels[log.step] || log.step}
              </span>
            )}
            <span className="tt-log-level">{log.level}</span>
            <span className="tt-log-msg">{log.message}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
