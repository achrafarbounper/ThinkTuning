/**
 * Page « Tableau de bord » — vue d'ensemble de la console.
 *
 * Cartes de statut (API, modèle, jobs actifs, versions entraînées),
 * raccourcis vers les autres pages et journal d'activité.
 */

import { useApp } from "../context/useApp";

const SENTIMENT_LABELS = { negative: "négatif", neutral: "neutre", positive: "positif" };

export default function HomePage({ onNavigate }) {
  const { config, health, healthError, models, modelsError, logs } = useApp();

  const healthDotClass = healthError
    ? "tt-dot tt-dot-red"
    : health?.model_available
    ? "tt-dot tt-dot-green"
    : "tt-dot tt-dot-amber";

  const stats = [
    {
      label: "État API",
      value: healthError ? "Injoignable" : health ? "En ligne" : "Connexion…",
      detail: config.baseUrl,
      dot: <span className={healthDotClass} aria-hidden="true" />,
    },
    {
      label: "Modèle de prédiction",
      value: healthError ? "—" : health?.model_available ? "Chargé" : "Non chargé",
      detail: models.length ? `${models.length} version(s) entraînée(s)` : "Aucune version",
    },
    {
      label: "Jobs actifs",
      value: health ? String(health.active_jobs ?? 0) : "—",
      detail: "Entraînements en cours",
    },
    {
      label: "Modèles listés",
      value: modelsError ? "Erreur" : String(models.length),
      detail: modelsError || "GET /models/details",
    },
  ];

  return (
    <>
      <header className="page-head">
        <h1>Tableau de bord</h1>
        <p>
          Analyse de sentiments FR/EN — supervision faible, entraînement, inférence.
        </p>
      </header>

      <div className="page-body">
        <div className="home-stats">
          {stats.map((s) => (
            <section className="tt-panel home-stat" key={s.label}>
              <p className="home-stat__label">
                {s.dot}
                {s.label}
              </p>
              <p className="home-stat__value">{s.value}</p>
              <p className="home-stat__detail" title={s.detail}>
                {s.detail}
              </p>
            </section>
          ))}
        </div>

        <section className="tt-panel">
          <div className="tt-panel-head">
            <h2>Raccourcis</h2>
          </div>
          <div className="home-links">
            <button type="button" className="tt-btn tt-btn-primary" onClick={() => onNavigate("analyse")}>
              Analyser un texte (FR/EN)
            </button>
            <button type="button" className="tt-btn tt-btn-ghost" onClick={() => onNavigate("assistant")}>
              Ouvrir l'assistant IA
            </button>
            <button type="button" className="tt-btn tt-btn-ghost" onClick={() => onNavigate("entrainement")}>
              Lancer un entraînement
            </button>
            <button type="button" className="tt-btn tt-btn-ghost" onClick={() => onNavigate("parametres")}>
              Configurer l'API
            </button>
          </div>
          {!config.apiKey && (
            <p className="tt-hint">
              Astuce : sans clé API, seul /health est accessible. Renseignez-la dans les
              paramètres pour débloquer modèles, prédiction et entraînement.
            </p>
          )}
        </section>

        <footer className="tt-panel">
          <div className="tt-panel-head">
            <h2>Activité récente</h2>
          </div>
          <ul className="home-logs">
            {logs.map((l) => (
              <li key={l.id} className={`tt-log-${l.type}`}>
                <span className="tt-mono">{new Date(l.ts).toLocaleTimeString()}</span> {l.text}
              </li>
            ))}
            {!logs.length && (
              <li className="tt-hint">Aucun évènement pour le moment.</li>
            )}
          </ul>
        </footer>
      </div>
    </>
  );
}

export { SENTIMENT_LABELS };
