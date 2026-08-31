/**
 * TrainingHistoryChart — SCRUM-73 : courbes de loss et de F1 par epoch,
 * par version de modèle (un run d'entraînement = une version).
 *
 * - Sélection d'un run (case à cocher) et comparaison de plusieurs runs
 *   simultanément (une série colorée par run).
 * - Deux graphiques Recharts LineChart : Loss de validation et F1 macro.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
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

const COLORS = ["#2563eb", "#16a34a", "#dc2626", "#ea580c", "#7c3aed", "#0891b2", "#be185d", "#4d7c0f"];

function shortId(jobId) {
  return jobId ? jobId.slice(0, 8) : "?";
}

export default function TrainingHistoryChart({ jobs = [], client, pushLog }) {
  const completedJobs = useMemo(
    () => jobs.filter((job) => job.status === "completed"),
    [jobs]
  );

  // Sélection : le dernier run terminé est pré-sélectionné par défaut.
  const [selectedIds, setSelectedIds] = useState([]);
  // Historiques chargés : { [job_id]: [{epoch, loss, f1_macro}] }
  const [histories, setHistories] = useState({});
  const [loading, setLoading] = useState(false);

  // Garde la sélection cohérente quand la liste des jobs évolue :
  // pré-sélectionne le dernier run terminé si rien n'est sélectionné.
  useEffect(() => {
    setSelectedIds((prev) => {
      const valid = prev.filter((id) =>
        completedJobs.some((job) => job.job_id === id)
      );
      if (valid.length) return valid;
      return completedJobs.length ? [completedJobs[0].job_id] : [];
    });
  }, [completedJobs]);

  // Charge l'historique des runs sélectionnés (une seule fois par run).
  useEffect(() => {
    if (!client || !selectedIds.length) return undefined;
    const missing = selectedIds.filter((id) => !histories[id]);
    if (!missing.length) return undefined;
    let cancelled = false;

    setLoading(true);
    Promise.all(
      missing.map((jobId) =>
        client
          .getTrainingHistory(jobId)
          .then((data) => ({ jobId, epochs: data?.epochs ?? [] }))
          .catch((err) => {
            if (pushLog) {
              pushLog(
                "error",
                `Historique des métriques indisponible pour ${shortId(jobId)} : ${err.message}`
              );
            }
            return { jobId, epochs: [] };
          })
      )
    ).then((results) => {
      if (cancelled) return;
      setHistories((prev) => {
        const next = { ...prev };
        for (const { jobId, epochs } of results) next[jobId] = epochs;
        return next;
      });
      setLoading(false);
    });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedIds, client]);

  const toggleRun = useCallback((jobId) => {
    setSelectedIds((prev) =>
      prev.includes(jobId) ? prev.filter((id) => id !== jobId) : [...prev, jobId]
    );
  }, []);

  // Fusionne les séries des runs sélectionnés : une ligne par epoch,
  // avec des clés composites "jobId-loss" / "jobId-f1" par run.
  const lossData = useMemo(() => {
    const byEpoch = new Map();
    for (const jobId of selectedIds) {
      for (const entry of histories[jobId] ?? []) {
        if (entry.loss == null) continue;
        const row = byEpoch.get(entry.epoch) ?? { epoch: entry.epoch };
        row[`${jobId}-loss`] = entry.loss;
        byEpoch.set(entry.epoch, row);
      }
    }
    return [...byEpoch.values()].sort((a, b) => a.epoch - b.epoch);
  }, [selectedIds, histories]);

  const f1Data = useMemo(() => {
    const byEpoch = new Map();
    for (const jobId of selectedIds) {
      for (const entry of histories[jobId] ?? []) {
        if (entry.f1_macro == null) continue;
        const row = byEpoch.get(entry.epoch) ?? { epoch: entry.epoch };
        row[`${jobId}-f1`] = entry.f1_macro;
        byEpoch.set(entry.epoch, row);
      }
    }
    return [...byEpoch.values()].sort((a, b) => a.epoch - b.epoch);
  }, [selectedIds, histories]);

  if (!completedJobs.length) {
    return (
      <p className="tt-hint">
        Aucun run terminé pour le moment : lancez un entraînement pour voir
        apparaître les courbes de loss et de F1.
      </p>
    );
  }

  return (
    <div className="tt-history-chart">
      <div className="tt-history-runs">
        <span className="tt-history-runs-label">Runs comparés :</span>
        {completedJobs.map((job, index) => {
          const color = COLORS[index % COLORS.length];
          const selected = selectedIds.includes(job.job_id);
          return (
            <label
              key={job.job_id}
              className={`tt-history-run ${selected ? "tt-history-run-selected" : ""}`}
            >
              <input
                type="checkbox"
                checked={selected}
                onChange={() => toggleRun(job.job_id)}
              />
              <span className="tt-history-run-dot" style={{ background: color }} />
              <span className="tt-mono">{shortId(job.job_id)}</span>
            </label>
          );
        })}
      </div>

      {loading && <p className="tt-hint">Chargement des métriques…</p>}

      {!loading && selectedIds.length > 0 && (
        <div className="tt-history-charts">
          <div className="tt-history-chart-block">
            <h4 className="tt-history-chart-title">Loss de validation par epoch</h4>
            {lossData.length ? (
              <ResponsiveContainer width="100%" height={260}>
                <LineChart data={lossData} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="epoch" allowDecimals={false} stroke="currentColor" />
                  <YAxis stroke="currentColor" />
                  <Tooltip />
                  <Legend />
                  {selectedIds.map((jobId, index) => (
                    <Line
                      key={jobId}
                      type="monotone"
                      dataKey={`${jobId}-loss`}
                      name={shortId(jobId)}
                      stroke={COLORS[index % COLORS.length]}
                      dot={false}
                      isAnimationActive={false}
                      connectNulls
                    />
                  ))}
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <p className="tt-hint">Pas de loss disponible pour ce(s) run(s).</p>
            )}
          </div>

          <div className="tt-history-chart-block">
            <h4 className="tt-history-chart-title">F1 macro (validation) par epoch</h4>
            {f1Data.length ? (
              <ResponsiveContainer width="100%" height={260}>
                <LineChart data={f1Data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="epoch" allowDecimals={false} stroke="currentColor" />
                  <YAxis stroke="currentColor" domain={[0, 1]} />
                  <Tooltip />
                  <Legend />
                  {selectedIds.map((jobId, index) => (
                    <Line
                      key={jobId}
                      type="monotone"
                      dataKey={`${jobId}-f1`}
                      name={shortId(jobId)}
                      stroke={COLORS[index % COLORS.length]}
                      dot={false}
                      isAnimationActive={false}
                      connectNulls
                    />
                  ))}
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <p className="tt-hint">Pas de F1 disponible pour ce(s) run(s).</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
