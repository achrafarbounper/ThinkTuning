/**
 * Page « Tableau de bord » — vue d'ensemble de la console.
 *
 * Cartes de statut (API, modèle, jobs actifs, versions entraînées),
 * raccourcis vers les autres pages et journal d'activité.
 */

import { useApp } from "../context/useApp";
import { Card } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { EmptyState } from "../components/ui/EmptyState";
import { formatTime } from "../lib/format";

interface HomePageProps {
  onNavigate?: (id: string) => void;
}

export default function HomePage({ onNavigate }: HomePageProps) {
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
        <div
          className="home-stats"
          aria-busy={(!health && !healthError) || (modelsError === null && models.length === 0)}
        >
          {stats.map((s) => (
            <Card className="home-stat" key={s.label}>
              <p className="home-stat__label">
                {s.dot}
                {s.label}
              </p>
              <p className="home-stat__value">{s.value}</p>
              <p className="home-stat__detail" title={s.detail}>
                {s.detail}
              </p>
            </Card>
          ))}
        </div>

        <Card title="Raccourcis">
          <div className="home-links">
            <Button variant="primary" onClick={() => onNavigate?.("analyse")}>
              Analyser un texte (FR/EN)
            </Button>
            <Button variant="ghost" onClick={() => onNavigate?.("assistant")}>
              Ouvrir l'assistant IA
            </Button>
            <Button variant="ghost" onClick={() => onNavigate?.("entrainement")}>
              Lancer un entraînement
            </Button>
            <Button variant="ghost" onClick={() => onNavigate?.("parametres")}>
              Configurer l'API
            </Button>
          </div>
          {!config.apiKey && (
            <p className="tt-hint">
              Astuce : sans clé API, seul /health est accessible. Renseignez-la dans les
              paramètres pour débloquer modèles, prédiction et entraînement.
            </p>
          )}
        </Card>

        <Card title="Activité récente" as="footer">
          {logs.length ? (
            <ul className="home-logs">
              {logs.map((l) => (
                <li key={l.id} className={`tt-log-${l.type}`}>
                  <span className="tt-mono">{formatTime(l.ts)}</span> {l.text}
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState>Aucun évènement pour le moment.</EmptyState>
          )}
        </Card>
      </div>
    </>
  );
}
