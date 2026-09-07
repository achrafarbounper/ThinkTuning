/**
 * ModelSanityPanel — Santé & nettoyage du répertoire experiments/models.
 */

import { useCallback, useState } from "react";
import type { SentimentApiClient } from "../api/sentimentApiClient";
import type { SanityReport, SanityVerdict } from "./types";

const VERDICT_META: Record<string, { label: string; className: string }> = {
  ok: { label: "Sain", className: "tt-sanity-verdict-ok" },
  untrained: { label: "Non entraîné", className: "tt-sanity-verdict-broken" },
  fallback_base_model: { label: "Fallback base model", className: "tt-sanity-verdict-broken" },
  model_unavailable: { label: "Illisible / incomplet", className: "tt-sanity-verdict-broken" },
  unknown: { label: "Non vérifié", className: "tt-sanity-verdict-unknown" },
  error: { label: "Erreur", className: "tt-sanity-verdict-unknown" },
};

const VERDICT_DELETEABLE = new Set<SanityVerdict>([
  "untrained",
  "fallback_base_model",
  "model_unavailable",
]);

function verdictMeta(verdict?: string) {
  return VERDICT_META[verdict ?? ""] || VERDICT_META.unknown;
}

interface ModelItem {
  name: string;
  path?: string;
  active?: boolean;
  created_at?: string;
}

export default function ModelSanityPanel({
  client,
  models = [],
  onModelsChanged,
  pushLog,
}: {
  client: SentimentApiClient;
  models: ModelItem[];
  onModelsChanged?: () => void;
  pushLog?: (level: string, message: string) => void;
}) {
  const [reports, setReports] = useState<Record<string, SanityReport>>({});
  const [checking, setChecking] = useState<Record<string, boolean>>({});
  const [scanning, setScanning] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const deleteOneSilent = useCallback(
    async (name: string): Promise<boolean> => {
      setChecking((prev) => ({ ...prev, [name]: true }));
      try {
        const res = (await client.deleteModel(name)) as { verdict?: string };
        setReports((prev) => {
          const next = { ...prev };
          delete next[name];
          return next;
        });
        pushLog?.("success", "Version " + name + " supprimée (verdict : " + res.verdict + ").");
        return true;
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        setError(msg);
        pushLog?.("error", "Suppression de " + name + " refusée : " + msg);
        return false;
      } finally {
        setChecking((prev) => ({ ...prev, [name]: false }));
      }
    },
    [client, pushLog]
  );

  const checkOne = useCallback(
    async (name: string): Promise<SanityReport> => {
      setChecking((prev) => ({ ...prev, [name]: true }));
      try {
        const report = (await client.getModelSanity(name)) as SanityReport;
        setReports((prev) => ({ ...prev, [name]: report }));
        pushLog?.(
          report.verdict === "ok" ? "success" : "error",
          "Sanity check " + name + " : " + report.verdict + "."
        );
        return report;
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        const report: SanityReport = { verdict: "error", detail: msg };
        setReports((prev) => ({ ...prev, [name]: report }));
        pushLog?.("error", "Sanity check " + name + " échoué : " + msg);
        return report;
      } finally {
        setChecking((prev) => ({ ...prev, [name]: false }));
      }
    },
    [client, pushLog]
  );

  const deleteOne = useCallback(
    async (name: string): Promise<void> => {
      const ok = await deleteOneSilent(name);
      if (ok) onModelsChanged?.();
    },
    [deleteOneSilent, onModelsChanged]
  );

  const scanAll = useCallback(async () => {
    setScanning(true);
    setError(null);
    try {
      for (const model of models) {
        await checkOne(model.name);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg);
    } finally {
      setScanning(false);
    }
  }, [models, checkOne]);

  const cleanAll = useCallback(async () => {
    setCleaning(true);
    setError(null);
    try {
      const broken: string[] = [];
      for (const model of models) {
        const report = reports[model.name];
        if (report && VERDICT_DELETEABLE.has(report.verdict as SanityVerdict) && !model.active) {
          broken.push(model.name);
        }
      }
      for (const name of broken) {
        await deleteOneSilent(name);
      }
      onModelsChanged?.();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg);
    } finally {
      setCleaning(false);
    }
  }, [models, reports, deleteOneSilent, onModelsChanged]);

  return (
    <section className="tt-sanity">
      <div className="tt-sanity-head">
        <h3 className="tt-subtitle">Santé des modèles</h3>
        <div className="tt-sanity-actions">
          <button type="button" className="tt-btn tt-btn-ghost" onClick={scanAll} disabled={scanning || cleaning}>
            {scanning ? "Analyse en cours…" : "Analyser tout"}
          </button>
          <button type="button" className="tt-btn tt-btn-danger" onClick={cleanAll} disabled={scanning || cleaning}>
            {cleaning ? "Nettoyage…" : "Nettoyer les défaillants"}
          </button>
        </div>
      </div>

      {error && <p className="tt-hint tt-hint-error">{error}</p>}

      <table className="tt-table">
        <caption className="sr-only">Santé des versions de modèle</caption>
        <thead>
          <tr>
            <th scope="col">Version</th>
            <th scope="col">Verdict</th>
            <th scope="col">Accuracy</th>
            <th scope="col">Actions</th>
          </tr>
        </thead>
        <tbody>
          {models.map((model) => {
            const report = reports[model.name];
            const meta = checking[model.name] ? { label: "Vérification…", className: "" } : verdictMeta(report?.verdict);
            const isChecking = !!checking[model.name];
            const isBroken = report && VERDICT_DELETEABLE.has(report.verdict as SanityVerdict);
            return (
              <tr key={model.name}>
                <td className="tt-mono">{model.name}</td>
                <td>
                  <span className={"tt-tag " + meta.className}>{isChecking ? "Vérification…" : meta.label}</span>
                  {report && report.verdict !== "ok" && (
                    <button type="button" className="tt-btn tt-btn-ghost tt-sanity-toggle" onClick={() => setExpanded(expanded === model.name ? null : model.name)}>
                      {expanded === model.name ? "Masquer" : "Détails"}
                    </button>
                  )}
                </td>
                <td className="tt-mono">{report?.accuracy != null ? Math.round(report.accuracy * 100) + " %" : "—"}</td>
                <td>
                  <div className="tt-sanity-actions-row">
                    <button type="button" className="tt-btn tt-btn-ghost" onClick={() => checkOne(model.name)} disabled={isChecking || scanning || cleaning}>Vérifier</button>
                    {isBroken && !model.active && (
                      <button type="button" className="tt-btn tt-btn-danger" onClick={() => deleteOne(model.name)} disabled={isChecking || scanning || cleaning}>Supprimer</button>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {models.map((model) => {
        if (expanded !== model.name) return null;
        const report = reports[model.name];
        if (!report) return null;
        const bad = (report.results || []).filter((r) => !r.correct);
        return (
          <div key={model.name + "-detail"} className="tt-sanity-detail">
            <p className="tt-hint">{report.detail}</p>
            {bad.length > 0 && (
              <table className="tt-table">
                <caption className="sr-only">Phrases mal classées par {model.name}</caption>
                <thead><tr><th scope="col">Phrase</th><th scope="col">Attendu</th><th scope="col">Prédit</th><th scope="col">Confiance</th></tr></thead>
                <tbody>
                  {bad.map((r) => (
                    <tr key={r.text}>
                      <td>{r.text}</td>
                      <td><span className="tt-tag">{r.lang}</span> {r.expected}</td>
                      <td>{r.predicted}</td>
                      <td className="tt-mono">{r.confidence.toFixed(3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        );
      })}
    </section>
  );
}
