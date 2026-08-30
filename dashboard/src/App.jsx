/**
 * Racine du dashboard ThinkTuning.
 *
 * Shell applicatif : barre latérale (menu) + zone de contenu affichant la
 * page active. La navigation utilise le hachage d'URL (#/analyse, …) pour
 * rester fonctionnelle au rechargement, sans dépendance externe.
 */

import { useEffect, useState } from "react";
import AppProvider from "./context/AppProvider";
import Sidebar from "./components/layout/Sidebar";
import HomePage from "./pages/HomePage";
import SentimentPage from "./pages/SentimentPage";
import ComparePage from "./pages/ComparePage";
import AssistantPage from "./pages/AssistantPage";
import TrainingPage from "./pages/TrainingPage";
import EvaluationPage from "./pages/EvaluationPage";
import SettingsPage from "./pages/SettingsPage";
import DriftPage from "./pages/DriftPage";

/** Table de routage : identifiant de menu → composant de page. */
const ROUTES = {
  dashboard: HomePage,
  analyse: SentimentPage,
  comparer: ComparePage,
  derive: DriftPage,
  assistant: AssistantPage,
  entrainement: TrainingPage,
  evaluation: EvaluationPage,
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
          <ActivePage onNavigate={navigate} />
        </main>
      </div>
    </AppProvider>
  );
}
