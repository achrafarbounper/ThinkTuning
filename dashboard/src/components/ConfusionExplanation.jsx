/**
 * ConfusionExplanation.jsx
 * ---------------------------------------------------------------------
 * Explication textuelle automatique des erreurs du modèle, dérivée
 * déterministiquement des données de GET /evaluate/confusion :
 * accuracy, rappels par classe et paires de confusion les plus fréquentes.
 */

const LABELS_FR = { negative: "négatif", neutral: "neutre", positive: "positif" };

function pct(v) {
  if (v == null) return "—";
  return `${(Math.round(v * 1000) / 10).toFixed(1)}%`;
}

export default function ConfusionExplanation({ data }) {
  if (!data) return null;

  const metrics = data.metrics || {};
  const accuracy = metrics.accuracy;
  const recalls = metrics.per_class_recall || {};
  const pairs = data.confusion_pairs || [];
  const mistakes = data.mistakes || [];

  // Classe au rappel le plus faible (parmi celles représentées).
  let weakest = null;
  for (const cls of data.labels || []) {
    const r = recalls[cls];
    if (r == null) continue;
    if (!weakest || r < weakest.recall) weakest = { cls, recall: r };
  }

  const topPair = pairs[0];

  return (
    <div className="tt-explain">
      <p>
        Sur <strong>{data.n}</strong> exemples, le modèle atteint{" "}
        <strong>{pct(accuracy)}</strong> d'accuracy. Les trois classes
        (<em>négatif</em>, <em>neutre</em>, <em>positif</em>) sont représentées en
        lignes (vrai) et en colonnes (prédit) ; la diagonale de la heatmap
        correspond aux bonnes prédictions, le reste aux erreurs.
      </p>

      {pairs.length > 0 ? (
        <>
          <p>
            La confusion la plus fréquente concerne les{" "}
            <span className="tt-explain-hl">
              {LABELS_FR[topPair.true_label]} prédits comme{" "}
              {LABELS_FR[topPair.pred_label]}
            </span>{" "}
            ({topPair.count} occurrence{topPair.count > 1 ? "s" : ""}). Au total,
            le modèle se trompe sur <strong>{mistakes.length}</strong> exemple
            {mistakes.length > 1 ? "s" : ""} de l'échantillon renvoyé.
          </p>
          {weakest && (
            <p>
              La classe au rappel le plus faible est{" "}
              <span className="tt-explain-hl">
                {LABELS_FR[weakest.cls] || weakest.cls} ({pct(weakest.recall)})
              </span>{" "}
              : c'est celle où le modèle « oublie » le plus de vrais positifs.
              C'est un bon point d'attention pour l'entraînement ou
              l'augmentation de données.
            </p>
          )}
        </>
      ) : (
        <p className="tt-explain-ok">
          Aucune confusion hors-diagonale : le modèle ne fait aucune erreur sur
          cet échantillon (accuracy de 100 %).
        </p>
      )}
    </div>
  );
}
