/**
 * Racine du dashboard ThinkTuning.
 *
 * Shell applicatif : barre latérale (menu) + zone de contenu affichant la
 * page active. La navigation utilise le hachage d'URL (#/analyse, …) pour
 * rester fonctionnelle au rechargement, sans dépendance externe.
 */

import { lazy, Suspense, useEffect, useRef, useState, type ComponentType } from "react";
import AppProvider from "./context/AppProvider";
import { ErrorBoundary } from "./components/ui";

// Sidebar chargé à la demande : prend en charge le CSS + les 10 icônes SVG
// (~30 KB sortis du chemin critique). Un fallback réservant l'espace évite
// toute reflow/CLS au swap.
const Sidebar = lazy(() => import("./components/layout/Sidebar"));

// Les pages sont chargées à la demande (code-splitting) : chaque page devient
// un bundle distinct, tiré au premier affichage. Le bundle initial reste minimal.
const HomePage = lazy(() => import("./pages/HomePage"));
const SentimentPage = lazy(() => import("./pages/SentimentPage"));
const ComparePage = lazy(() => import("./pages/ComparePage"));
const AssistantPage = lazy(() => import("./pages/AssistantPage"));
const TrainingPage = lazy(() => import("./pages/TrainingPage"));
const EvaluationPage = lazy(() => import("./pages/EvaluationPage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));
const DriftPage = lazy(() => import("./pages/DriftPage"));
const PipelinePage = lazy(() => import("./pages/PipelinePage"));
const MonitoringPage = lazy(() => import("./pages/MonitoringPage"));
const AnnotationPage = lazy(() => import("./pages/AnnotationPage"));
const FlowMapPage = lazy(() => import("./pages/FlowMapPage"));

/** Table de routage : identifiant de menu → composant de page. */
const ROUTES: Record<string, ComponentType<{ onNavigate?: (id: string) => void }>> = {
  dashboard: HomePage,
  analyse: SentimentPage,
  comparer: ComparePage,
  derive: DriftPage,
  assistant: AssistantPage,
  entrainement: TrainingPage,
  pipeline: PipelinePage,
  annotation: AnnotationPage,
  evaluation: EvaluationPage,
  monitoring: MonitoringPage,
  flowmap: FlowMapPage,
  parametres: SettingsPage,
} as unknown as Record<string, ComponentType<{ onNavigate?: (id: string) => void }>>;

/** Lit la page active depuis l'URL (#/xxx), avec repli sur le tableau de bord. */
function pageFromHash(): string {
  const id = window.location.hash.replace(/^#\/?/, "");
  return ROUTES[id] ? id : "dashboard";
}

export default function App() {
  const [page, setPage] = useState(pageFromHash);
  const mainRef = useRef<HTMLElement>(null);

  // Synchronise l'état si l'utilisateur modifie le hachage (retour navigateur…).
  useEffect(() => {
    const onHashChange = () => setPage(pageFromHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const navigate = (id: string) => {
    window.location.hash = `/${id}`;
    setPage(id);
  };

  // Au changement de « page » : déplace le focus sur <main> pour que les
  // lecteurs d'écran annoncent la nouvelle vue (WCAG 2.4.3 — focus order).
  useEffect(() => {
    mainRef.current?.focus({ preventScroll: true });
  }, [page]);

  const ActivePage = ROUTES[page];

  return (
    <AppProvider>
      <a className="skip-link" href="#contenu">
        Aller au contenu
      </a>
      <div className="app-shell">
        <Suspense fallback={<SidebarFallback />}>
          <Sidebar page={page} onNavigate={navigate} />
        </Suspense>
        <main
          id="contenu"
          className="app-main"
          ref={mainRef}
          tabIndex={-1}
        >
          <ErrorBoundary>
            <Suspense fallback={<PageFallback />}>
              <ActivePage onNavigate={navigate} />
            </Suspense>
          </ErrorBoundary>
        </main>
      </div>
    </AppProvider>
  );
}

/** Skeleton léger affiché pendant le chargement asynchrone d'une page. */
function PageFallback() {
  return (
    <div className="page-fallback" role="status" aria-label="Chargement de la page">
      <div className="page-fallback__bar" />
      <div className="page-fallback__card" />
      <div className="page-fallback__card" />
    </div>
  );
}

/**
 * Réserve l'espace exact de la sidebar (250px pleine hauteur) pendant son
 * chargement différé, afin d'éviter tout reflow/CLS au moment du swap.
 */
function SidebarFallback() {
  return <aside className="sidebar sidebar--loading" aria-hidden="true" />;
}
