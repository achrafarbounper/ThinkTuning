/**
 * Sidebar — menu de navigation latéral du dashboard ThinkTuning.
 *
 * Navigation par hachage (#/analyse, #/assistant…) gérée dans App.jsx :
 * `page` est l'identifiant actif, `onNavigate(id)` met à jour l'URL.
 * L'état de santé de l'API (contexte) est affiché en bas du menu.
 */

import { useApp } from "../../context/useApp";
import "./Sidebar.css";

function Icon({ children }) {
  return (
    <svg
      className="sidebar__icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

const MENU_ITEMS = [
  {
    id: "dashboard",
    label: "Tableau de bord",
    hint: "Vue d'ensemble",
    icon: (
      <Icon>
        <rect x="3" y="3" width="7" height="7" rx="1" />
        <rect x="14" y="3" width="7" height="7" rx="1" />
        <rect x="3" y="14" width="7" height="7" rx="1" />
        <rect x="14" y="14" width="7" height="7" rx="1" />
      </Icon>
    ),
  },
  {
    id: "analyse",
    label: "Analyse de sentiments",
    hint: "Prédiction FR/EN",
    icon: (
      <Icon>
        <line x1="18" y1="20" x2="18" y2="10" />
        <line x1="12" y1="20" x2="12" y2="4" />
        <line x1="6" y1="20" x2="6" y2="14" />
      </Icon>
    ),
  },
  {
    id: "comparer",
    label: "Comparer",
    hint: "Deux modèles, un texte",
    icon: (
      <Icon>
        <path d="m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z" />
        <path d="m2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z" />
        <path d="M7 21h10" />
        <path d="M12 3v18" />
        <path d="M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2" />
      </Icon>
    ),
  },
  {
    id: "assistant",
    label: "Assistant IA",
    hint: "Chat en streaming",
    icon: (
      <Icon>
        <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
      </Icon>
    ),
  },
  {
    id: "entrainement",
    label: "Entraînement",
    hint: "Fine-tuning & jobs",
    icon: (
      <Icon>
        <circle cx="12" cy="12" r="10" />
        <circle cx="12" cy="12" r="5" />
        <line x1="12" y1="12" x2="12" y2="12" />
      </Icon>
    ),
  },
  {
    id: "evaluation",
    label: "Évaluation",
    hint: "Confusion & comparaison",
    icon: (
      <Icon>
        <rect x="3" y="3" width="7" height="7" rx="1" />
        <rect x="14" y="3" width="7" height="7" rx="1" />
        <path d="m14 17 3 3 4-4" />
        <line x1="3" y1="17" x2="11" y2="17" />
      </Icon>
    ),
  },
  {
    id: "parametres",
    label: "Paramètres",
    hint: "Connexion & préférences",
    icon: (
      <Icon>
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
      </Icon>
    ),
  },
];

export default function Sidebar({ page, onNavigate }) {
  const { health, healthError } = useApp();

  const healthDotClass = healthError
    ? "tt-dot tt-dot-red"
    : health?.model_available
    ? "tt-dot tt-dot-green"
    : "tt-dot tt-dot-amber";

  const statusLabel = healthError
    ? "API injoignable"
    : health
    ? health.model_available
      ? "Modèle chargé"
      : "Aucun modèle"
    : "Connexion…";

  return (
    <aside className="sidebar">
      <div className="sidebar__brand">
        <span className="sidebar__mark">TT</span>
        <div className="sidebar__brand-text">
          <strong>ThinkTuning</strong>
          <small>console</small>
        </div>
      </div>

      <nav className="sidebar__nav" aria-label="Navigation principale">
        {MENU_ITEMS.map((item) => (
          <button
            key={item.id}
            type="button"
            className={`sidebar__item${page === item.id ? " sidebar__item--active" : ""}`}
            onClick={() => onNavigate(item.id)}
            aria-current={page === item.id ? "page" : undefined}
            title={item.label}
          >
            {item.icon}
            <span className="sidebar__item-text">
              <span className="sidebar__item-label">{item.label}</span>
              <span className="sidebar__item-hint">{item.hint}</span>
            </span>
          </button>
        ))}
      </nav>

      <div className="sidebar__footer">
        <span className={healthDotClass} aria-hidden="true" />
        <span className="sidebar__footer-text" title={healthError || undefined}>
          {statusLabel}
        </span>
      </div>
    </aside>
  );
}
