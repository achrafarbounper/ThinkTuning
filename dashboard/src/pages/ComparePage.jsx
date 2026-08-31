/**
 * Page « Comparer » — même texte soumis à deux versions de modèles.
 *
 * Permet de comparer côte à côte les prédictions (sentiment + confiance)
 * de deux versions choisies via des menus déroulants alimentés par
 * GET /models (liste des versions exposée par le contexte applicatif).
 * Un indicateur visuel signale la divergence éventuelle entre modèles.
 */

import { useEffect, useMemo, useState } from "react";
import { useApp } from "../context/useApp";
import ModelVersionSelector from "../components/ModelVersionSelector";
import { sentimentLabel } from "../lib/sentiment";

/** Paires de sentiments franchement opposées (accord impossible). */
const OPPOSED_PAIRS = new Set(["positive|negative", "negative|positive"]);

function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
      <polyline points="22 4 12 14.01 9 11.01" />
    </svg>
  );
}

function WarnIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  );
}

/** Carte résultat d'un côté de la comparaison (Modèle A ou Modèle B). */
function ModelResultCard({ side, modelName, state }) {
  return (
    <section
      className={`tt-panel tt-compare-card${
        state.status === "error" ? " tt-compare-card--error" : ""
      }`}
    >
      <div className="tt-panel-head">
        <h2>{side}</h2>
        <span className="tt-tag" title={modelName || undefined}>
          {modelName || "Le plus récent (auto)"}
        </span>
      </div>

      {state.status === "idle" && (
        <p className="tt-hint">Lancez une comparaison pour voir le résultat.</p>
      )}
      {state.status === "loading" && <p className="tt-hint">Prédiction en cours…</p>}
      {state.status === "error" && <p className="tt-hint tt-hint-error">{state.error}</p>}

      {state.status === "done" && state.result && (
        <div className="tt-compare-result">
          <span className={`tt-badge tt-badge-${state.result.sentiment}`}>
            {sentimentLabel(state.result.sentiment)}
          </span>
          <span className="tt-mono tt-compare-conf-value">
            {(state.result.confidence * 100).toFixed(1)}%
          </span>
          <span
            className="tt-confidence"
            title={`Confiance : ${(state.result.confidence * 100).toFixed(1)}%`}
          >
            <span
              className={`tt-confidence-fill tt-fill-${state.result.sentiment}`}
              style={{ width: `${Math.round(state.result.confidence * 100)}%` }}
            />
          </span>
        </div>
      )}
    </section>
  );
}

const IDLE = { status: "idle" };
const LOADING = { status: "loading" };

export default function ComparePage() {
  const { client, models, modelsError, refreshModels } = useApp();

  const [modelA, setModelA] = useState("");
  const [modelB, setModelB] = useState("");
  const [text, setText] = useState("");
  const [stateA, setStateA] = useState(IDLE);
  const [stateB, setStateB] = useState(IDLE);
  const [comparing, setComparing] = useState(false);

  // Repli : recharge la liste des versions (GET /models) si le contexte,
  // rafraîchi périodiquement, n'a encore rien remonté au premier rendu.
  useEffect(() => {
    if (!models.length && !modelsError) refreshModels();
  }, [models.length, modelsError, refreshModels]);

  const sameSelection = modelA !== "" && modelA === modelB;
  const canCompare = text.trim().length > 0 && !comparing && !sameSelection;

  const handleSwap = () => {
    setModelA(modelB);
    setModelB(modelA);
  };

  // Verdict calculé dès que les deux côtés ont abouti.
  const verdict = useMemo(() => {
    if (stateA.status !== "done" || stateB.status !== "done") return null;
    const a = stateA.result;
    const b = stateB.result;
    if (!a || !b) return null;
    const identical = a.sentiment === b.sentiment;
    return {
      identical,
      opposed: !identical && OPPOSED_PAIRS.has(`${a.sentiment}|${b.sentiment}`),
      confDiff: Math.abs(a.confidence - b.confidence),
      sentimentA: sentimentLabel(a.sentiment),
      sentimentB: sentimentLabel(b.sentiment),
    };
  }, [stateA, stateB]);

  // Prédiction simultanée : POST /predict?model=A et ?model=B en parallèle.
  // allSettled permet d'afficher un résultat même si un seul modèle échoue.
  const handleCompare = async (e) => {
    e.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || comparing || sameSelection) return;

    setComparing(true);
    setStateA(LOADING);
    setStateB(LOADING);

    const [resA, resB] = await Promise.allSettled([
      client.predict([trimmed], modelA || undefined),
      client.predict([trimmed], modelB || undefined),
    ]);

    if (resA.status === "fulfilled") {
      setStateA({ status: "done", result: resA.value.results[0] });
    } else {
      setStateA({ status: "error", error: resA.reason?.message || "Erreur inconnue." });
    }

    if (resB.status === "fulfilled") {
      setStateB({ status: "done", result: resB.value.results[0] });
    } else {
      setStateB({ status: "error", error: resB.reason?.message || "Erreur inconnue." });
    }

    setComparing(false);
  };

  const verdictClass = verdict
    ? verdict.identical
      ? "tt-compare-verdict--ok"
      : verdict.opposed
        ? "tt-compare-verdict--opposed"
        : "tt-compare-verdict--diff"
    : "";

  return (
    <>
      <header className="page-head">
        <h1>Comparer</h1>
        <p>Testez le même texte sur deux versions de modèles et visualisez leurs différences.</p>
      </header>

      <div className="page-body">
        <section className="tt-panel">
          <div className="tt-panel-head">
            <h2>Versions de modèles</h2>
            {modelsError && (
              <span className="tt-hint tt-hint-error">Modèles : {modelsError}</span>
            )}
          </div>

          <div className="tt-compare-selectors">
            <ModelVersionSelector
              models={models}
              activeModel={modelA}
              onModelChange={setModelA}
              loading={!models.length}
              label="Modèle A"
            />
            <button
              type="button"
              className="tt-btn tt-btn-ghost tt-compare-swap"
              onClick={handleSwap}
              title="Inverser les modèles A et B"
            >
              ⇄
            </button>
            <ModelVersionSelector
              models={models}
              activeModel={modelB}
              onModelChange={setModelB}
              loading={!models.length}
              label="Modèle B"
            />
          </div>

          {sameSelection && (
            <p className="tt-hint tt-hint-error">
              Sélectionnez deux versions différentes pour comparer.
            </p>
          )}
          {!models.length && !modelsError && (
            <p className="tt-hint">
              Aucune version disponible pour le moment. Entraînez un modèle ou vérifiez la
              configuration API dans Paramètres.
            </p>
          )}
        </section>

        <section className="tt-panel">
          <div className="tt-panel-head">
            <h2>Texte à tester</h2>
          </div>
          <form className="tt-form" onSubmit={handleCompare}>
            <textarea
              rows={4}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Saisissez le texte à soumettre aux deux modèles…"
            />
            <div className="tt-compare-actions">
              <button className="tt-btn tt-btn-primary" type="submit" disabled={!canCompare}>
                {comparing ? "Comparaison…" : "Comparer"}
              </button>
              <span className="tt-hint">La prédiction est lancée simultanément sur A et B.</span>
            </div>
          </form>
        </section>

        {verdict && (
          <div className={`tt-compare-verdict ${verdictClass}`} role="status">
            {verdict.identical ? <CheckIcon /> : <WarnIcon />}
            <span>
              {verdict.identical
                ? `Modèles d'accord — sentiment ${verdict.sentimentA} des deux côtés`
                : verdict.opposed
                  ? `Opposition franche : ${verdict.sentimentA} (A) vs ${verdict.sentimentB} (B)`
                  : `Divergence : ${verdict.sentimentA} (A) vs ${verdict.sentimentB} (B)`}
              {!verdict.identical &&
                ` · écart de confiance : ${(verdict.confDiff * 100).toFixed(1)} pts`}
            </span>
          </div>
        )}

        <div className="tt-compare-grid">
          <ModelResultCard side="Modèle A" modelName={modelA} state={stateA} />
          <ModelResultCard side="Modèle B" modelName={modelB} state={stateB} />
        </div>
      </div>
    </>
  );
}


