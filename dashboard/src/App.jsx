/**
 * Racine du dashboard ThinkTuning.
 *
 * Shell applicatif : barre latérale (menu) + zone de contenu affichant la
 * page active. La navigation utilise le hachage d'URL (#/analyse, …) pour
 * rester fonctionnelle au rechargement, sans dépendance externe.
 */

import { lazy, Suspense, useEffect, useState } from "react";
import AppProvider from "./context/AppProvider";
import Sidebar from "./components/layout/Sidebar";
import { ErrorBoundary } from "./components/ui";

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

/** Table de routage : identifiant de menu → composant de page. */
const ROUTES = {
  dashboard: HomePage,
  analyse: SentimentPage,
  comparer: ComparePage,
  derive: DriftPage,
  assistant: AssistantPage,
  entrainement: TrainingPage,
  pipeline: PipelinePage,
  evaluation: EvaluationPage,
  monitoring: MonitoringPage,
  parametres: SettingsPage,
};

/** Lit la page active depuis l'URL (#/xxx), avec repli sur le tableau de bord. */
function pageFromHash() {
  const id = window.location.hash.replace(/^#\/?/, "");
  return ROUTES[id] ? id : "dashboard";
}

export default function App() {
  const [page, setPage] = useState(pageFromHash);

  // Synchronise l'état si l'utilisateur modifie le hachage (retour navigateur…).
  useEffect(() => {
    const onHashChange = () => setPage(pageFromHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const navigate = (id) => {
    window.location.hash = `/${id}`;
    setPage(id);
  };

  const ActivePage = ROUTES[page];

  return (
    <AppProvider>
      <div className="app-shell">
        <Sidebar page={page} onNavigate={navigate} />
        <main className="app-main">
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
