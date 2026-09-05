/**
 * Sidebar — menu de navigation latéral du dashboard ThinkTuning.
 *
 * Navigation par hachage (#/analyse, #/assistant…) gérée dans App.tsx :
 * `page` est l'identifiant actif. Les items sont de vrais liens <a href> :
 * navigation native (retour navigateur, ouverture dans un onglet) et
 * sémantique correcte pour les lecteurs d'écran.
 * Les entrées sont regroupées par thème en accordéons (MENU_GROUPS) ; le
 * groupe de la page active est maintenu ouvert automatiquement.
 * L'état de santé de l'API (contexte) est affiché en bas du menu.
 */

import { useEffect, useState, type ReactNode } from "react";
import { useApp } from "../../context/useApp";
import "./Sidebar.css";

function Icon({ children }: { children: ReactNode }) {
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

interface MenuItem {
  id: string;
  /** Identifiant du groupe d'accordéon contenant l'entrée (cf. MENU_GROUPS). */
  group: string;
  label: string;
  hint: string;
  icon: ReactNode;
}

interface MenuGroup {
  id: string;
  label: string;
  icon: ReactNode;
}

/** Groupes thématiques du menu, dans l'ordre d'affichage. */
const MENU_GROUPS: MenuGroup[] = [
  {
    id: "analyse",
    label: "Analyse & Modèles",
    icon: (
      <Icon>
        <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
      </Icon>
    ),
  },
  {
    id: "labo",
    label: "Laboratoire",
    icon: (
      <Icon>
        <path d="M10 2v7.5a2 2 0 0 1-.2.9L4.7 20.5a1 1 0 0 0 .9 1.5h12.8a1 1 0 0 0 .9-1.5L14.2 10.4a2 2 0 0 1-.2-.9V2" />
        <path d="M8.5 2h7" />
        <path d="M7 16h10" />
      </Icon>
    ),
  },
  {
    id: "outils",
    label: "Outils",
    icon: (
      <Icon>
        <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
      </Icon>
    ),
  },
  {
    id: "configuration",
    label: "Configuration",
    icon: (
      <Icon>
        <line x1="4" y1="21" x2="4" y2="14" />
        <line x1="4" y1="10" x2="4" y2="3" />
        <line x1="12" y1="21" x2="12" y2="12" />
        <line x1="12" y1="8" x2="12" y2="3" />
        <line x1="20" y1="21" x2="20" y2="16" />
        <line x1="20" y1="12" x2="20" y2="3" />
        <line x1="1" y1="14" x2="7" y2="14" />
        <line x1="9" y1="8" x2="15" y2="8" />
        <line x1="17" y1="16" x2="23" y2="16" />
      </Icon>
    ),
  },
];

const MENU_ITEMS: MenuItem[] = [
  {
    id: "dashboard",
    group: "analyse",
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
    group: "analyse",
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
    id: "intention",
    group: "analyse",
    label: "Classification d'intention",
    hint: "Chat vs action",
    icon: (
      <Icon>
        <circle cx="6" cy="6" r="3" />
        <circle cx="6" cy="18" r="3" />
        <line x1="9" y1="6" x2="21" y2="6" />
        <line x1="9" y1="18" x2="21" y2="18" />
        <path d="M9 12h12" />
      </Icon>
    ),
  },
  {
    id: "comparer",
    group: "analyse",
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
    id: "derive",
    group: "analyse",
    label: "Dérive",
    hint: "Drift entre deux batches",
    icon: (
      <Icon>
        <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
      </Icon>
    ),
  },
  {
    id: "assistant",
    group: "outils",
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
    group: "labo",
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
    id: "annotation",
    group: "labo",
    label: "Annotation",
    hint: "Active learning & cycle",
    icon: (
      <Icon>
        <path d="M12 20h9" />
        <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
      </Icon>
    ),
  },
  {
    id: "pipeline",
    group: "labo",
    label: "Pipeline LLM",
    hint: "Label → filtrage → LoRA",
    icon: (
      <Icon>
        <line x1="4" y1="6" x2="12" y2="6" />
        <line x1="4" y1="12" x2="20" y2="12" />
        <line x1="4" y1="18" x2="16" y2="18" />
        <circle cx="20" cy="6" r="1.5" />
        <circle cx="16" cy="18" r="1.5" />
      </Icon>
    ),
  },
  {
    id: "evaluation",
    group: "labo",
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
    id: "monitoring",
    group: "outils",
    label: "Monitoring",
    hint: "Métriques Prometheus live",
    icon: (
      <Icon>
        <line x1="22" y1="12" x2="18" y2="12" />
        <line x1="6" y1="12" x2="2" y2="12" />
        <line x1="12" y1="6" x2="12" y2="2" />
        <line x1="12" y1="22" x2="12" y2="18" />
        <path d="M5 3 3 5M21 19l-2-2M5 21l-2-2M21 5l-2-2" />
        <circle cx="12" cy="12" r="3" />
      </Icon>
    ),
  },
  {
    id: "flowmap",
    group: "outils",
    label: "Agent Flow Map",
    hint: "Pipeline IA animé",
    icon: (
      <Icon>
        <circle cx="5" cy="6" r="3" />
        <circle cx="19" cy="6" r="3" />
        <circle cx="12" cy="19" r="3" />
        <path d="M7.2 7.7 10.8 16M16.8 7.7 13.2 16" />
      </Icon>
    ),
  },
  {
    id: "parametres",
    group: "configuration",
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
interface SidebarProps {
  /** Identifiant de la page active. */
  page: string;
  /** Conservé pour compatibilité (l'anchor fait la navigation). */
  onNavigate?: (id: string) => void;
}

export default function Sidebar({ page }: SidebarProps) {
  const { health, healthError } = useApp();

  // Accordéons : chaque groupe se replie indépendamment. À l'initialisation,
  // seul le groupe contenant la page active est déplié (menu compact).
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(() => {
    const activeGroup = MENU_ITEMS.find((item) => item.id === page)?.group;
    return Object.fromEntries(
      MENU_GROUPS.map((group) => [group.id, group.id !== activeGroup]),
    );
  });

  // Navigation hors clic (retour navigateur, lien direct) : rouvre le groupe
  // de la page affichée pour que l'entrée active reste visible.
  useEffect(() => {
    const activeGroup = MENU_ITEMS.find((item) => item.id === page)?.group;
    if (activeGroup) {
      setCollapsed((prev) =>
        prev[activeGroup] ? { ...prev, [activeGroup]: false } : prev,
      );
    }
  }, [page]);

  const toggleGroup = (groupId: string) =>
    setCollapsed((prev) => ({ ...prev, [groupId]: !prev[groupId] }));

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
        {MENU_GROUPS.map((group) => {
          const items = MENU_ITEMS.filter((item) => item.group === group.id);
          const isCollapsed = collapsed[group.id] ?? false;
          return (
            <div
              key={group.id}
              className={`sidebar__group${isCollapsed ? " sidebar__group--collapsed" : ""}`}
            >
              <button
                type="button"
                className="sidebar__group-header"
                onClick={() => toggleGroup(group.id)}
                aria-expanded={!isCollapsed}
                aria-controls={`sidebar-group-${group.id}`}
              >
                {group.icon}
                <span className="sidebar__group-label">{group.label}</span>
                <svg
                  className="sidebar__group-chevron"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <path d="m6 9 6 6 6-6" />
                </svg>
              </button>
              <div
                className="sidebar__group-items"
                id={`sidebar-group-${group.id}`}
              >
                {items.map((item) => (
                  <a
                    key={item.id}
                    href={`#/${item.id}`}
                    className={`sidebar__item${page === item.id ? " sidebar__item--active" : ""}`}
                    aria-current={page === item.id ? "page" : undefined}
                    title={item.label}
                  >
                    {item.icon}
                    <span className="sidebar__item-text">
                      <span className="sidebar__item-label">{item.label}</span>
                      <span className="sidebar__item-hint">{item.hint}</span>
                    </span>
                  </a>
                ))}
              </div>
            </div>
          );
        })}
      </nav>

      <div className="sidebar__footer" aria-busy={!health && !healthError}>
        <span className={healthDotClass} aria-hidden="true" />
        <span className="sidebar__footer-text" title={healthError || undefined}>
          {statusLabel}
        </span>
      </div>
    </aside>
  );
}
