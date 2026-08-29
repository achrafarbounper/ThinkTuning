/**
 * ErrorLog.jsx
 * ---------------------------------------------------------------------
 * Journal des erreurs : liste défilante des exemples mal classés renvoyés
 * par GET /evaluate/confusion (champ `mistakes`).
 *
 * Chaque ligne affiche le texte, le sentiment vrai → prédit (badges) et la
 * confiance de la prédiction.
 */

const LABELS_FR = { negative: "Négatif", neutral: "Neutre", positive: "Positif" };

function truncate(text, max = 140) {
  const s = String(text ?? "");
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

export default function ErrorLog({ mistakes = [], maxShown = 50 }) {
  const shown = mistakes.slice(0, maxShown);

  return (
    <div className="tt-errlog">
      <div className="tt-errlog-head">
        <strong>Journal des erreurs</strong>
        <span className="tt-errlog-count">
          {mistakes.length} mal classé{mistakes.length > 1 ? "s" : ""}
          {mistakes.length > maxShown ? ` (${maxShown} affichés)` : ""}
        </span>
      </div>

      {shown.length === 0 ? (
        <p className="tt-hint">Aucune erreur à signaler sur cet échantillon.</p>
      ) : (
        <ul className="tt-errlog-list">
          {shown.map((m, i) => (
            <li key={i} className="tt-errlog-item">
              <span className="tt-errlog-text" title={m.text}>
                {truncate(m.text)}
              </span>
              <span className="tt-errlog-flow">
                <span className={`tt-badge tt-badge-${m.true_label}`}>
                  {LABELS_FR[m.true_label] || m.true_label}
                </span>
                <span className="tt-errlog-arrow">→</span>
                <span className={`tt-badge tt-badge-${m.pred_label}`}>
                  {LABELS_FR[m.pred_label] || m.pred_label}
                </span>
              </span>
              <span className="tt-errlog-conf tt-mono">
                {((m.confidence ?? 0) * 100).toFixed(0)}%
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
