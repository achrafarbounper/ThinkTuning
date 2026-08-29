/**
 * EvaluationPage.jsx
 * ---------------------------------------------------------------------
 * Page « Évaluation » du dashboard : matrice de confusion (heatmap) et
 * comparaison de deux versions de modèles (panneau premium v1 vs v2).
 *
 * Toutes les données proviennent de GET /evaluate/confusion (métriques,
 * matrice et erreurs par classe calculées côté serveur).
 */

import { useEffect, useState } from "react";
import { useApp } from "../context/useApp";
import ModelVersionSelector from "../components/ModelVersionSelector";
import ConfusionHeatmap from "../components/ConfusionHeatmap";
import ConfusionExplanation from "../components/ConfusionExplanation";
import ErrorLog from "../components/ErrorLog";
import ModelComparisonPanel from "../components/ModelComparisonPanel";

const SENTIMENT_LABELS_FR = { negative: "Négatif", neutral: "Neutre", positive: "Positif" };

function useConfusion(client, model, limit = 300) {
  const [result, setResult] = useState({ status: "idle", data: null, error: null, forModel: null });

  const currentKey = model || null;

  useEffect(() => {
    let cancelled = false;
    client
      .getConfusion({ model: model || null, limit })
      .then((data) => {
        if (!cancelled)
          setResult({ status: "done", data, error: null, forModel: model || null });
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
  }, [client, model, limit]);

  // L'état « loading » est dérivé : on charge tant que la donnée en cache ne
  // correspond pas au modèle courant (évite un setState synchrone dans l'effet).
  if (result.forModel !== currentKey) {
    return { status: "loading", data: null, error: null };
  }
  return result;
}

export default function EvaluationPage() {
  const { client, models, modelsError } = useApp();

  const [heatModel, setHeatModel] = useState("");
  const [modelA, setModelA] = useState("");
  const [modelB, setModelB] = useState("");

  const heat = useConfusion(client, heatModel);
  const evalA = useConfusion(client, modelA);
  const evalB = useConfusion(client, modelB);

  const errorMsg = heat?.error || evalA?.error || evalB?.error;

  return (
    <>
      <header className="page-head">
        <h1>Évaluation</h1>
        <p>
          Matrice de confusion et erreurs par classe, calculées sur un échantillon
          de référence — comparaison de deux versions de modèles.
        </p>
      </header>

      <div className="page-body tt-eval">
        {!models.length && !modelsError && (
          <p className="tt-hint">
            Aucune version de modèle disponible pour le moment. Entraînez un modèle
            ou vérifiez la configuration API dans Paramètres.
          </p>
        )}
        {errorMsg && <p className="tt-hint tt-hint-error">{errorMsg}</p>}

        {/* --- Matrice de confusion --- */}
        <section className="tt-panel tt-eval-panel">
          <div className="tt-panel-head">
            <h2>Erreurs par classe · matrice de confusion</h2>
            <span className="tt-tag">GET /evaluate/confusion</span>
          </div>

          <div className="tt-eval-controls">
            <ModelVersionSelector
              models={models}
              activeModel={heatModel}
              onModelChange={setHeatModel}
              loading={!models.length}
              label="Modèle évalué"
            />
            {heat?.data?.n > 0 && (
              <p className="tt-eval-meta">
                {heat.data.n} exemples ·{" "}
                <span className="tt-mono">{((heat.data.metrics?.accuracy ?? 0) * 100).toFixed(1)}%</span>{" "}
                accuracy
              </p>
            )}
          </div>



          {heat?.status === "loading" && (
            <p className="tt-hint">Évaluation en cours…</p>
          )}
          {heat?.status === "done" && heat.data && (
            <>
              <ConfusionExplanation data={heat.data} />
              <ConfusionHeatmap data={heat.data} />
            </>
          )}
          {heat?.status === "idle" && <p className="tt-hint">Sélectionnez un modèle.</p>}

          {/* Légende + erreurs par classe */}
          {heat?.status === "done" && heat?.data?.errors_by_class && (
            <div className="tt-eval-footer">
              <div className="tt-legend">
                <span className="tt-legend-item">
                  <span className="tt-legend-swatch tt-legend-correct" /> bonne prédiction
                </span>
                <span className="tt-legend-item">
                  <span className="tt-legend-swatch tt-legend-error" /> erreur
                </span>
              </div>
              <div className="tt-errlist">
                {heat.data.errors_by_class.map((e) => (
                  <span key={e.label} className="tt-errchip" title={`${SENTIMENT_LABELS_FR[e.label] || e.label}`}>
                    {SENTIMENT_LABELS_FR[e.label] || e.label} :{" "}
                    <strong>{e.errors}</strong>/{e.total}
                  </span>
                ))}
              </div>
            </div>
          )}

          {heat?.status === "done" && (
            <ErrorLog mistakes={heat.data?.mistakes || []} />
          )}
        </section>

        {/* --- Comparaison v1 vs v2 --- */}
        <section className="tt-panel tt-eval-panel">
          <div className="tt-panel-head">
            <h2>Comparaison v1 vs v2</h2>
            <span className="tt-tag">Métriques sur échantillon</span>
          </div>

          <div className="tt-compare-selectors">
            <ModelVersionSelector
              models={models}
              activeModel={modelA}
              onModelChange={setModelA}
              loading={!models.length}
              label="v1 (Modèle A)"
            />
            <button
              type="button"
              className="tt-btn tt-btn-ghost tt-compare-swap"
              onClick={() => {
                setModelA(modelB);
                setModelB(modelA);
              }}
              title="Inverser v1 et v2"
            >
              ⇄
            </button>
            <ModelVersionSelector
              models={models}
              activeModel={modelB}
              onModelChange={setModelB}
              loading={!models.length}
              label="v2 (Modèle B)"
            />
          </div>

          {modelA && modelB && modelA === modelB && (
            <p className="tt-hint tt-hint-error">
              Sélectionnez deux versions différentes pour comparer.
            </p>
          )}

          <ModelComparisonPanel client={client} modelA={modelA} modelB={modelB} />
        </section>
      </div>
    </>
  );
}
