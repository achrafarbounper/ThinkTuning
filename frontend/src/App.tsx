/**
 * Racine de l'application ThinkTuning.
 *
 * Aiguillage global :
 *   - aucune session valide → écran d'authentification (POST /api/v1/auth/token) ;
 *   - session valide        → dashboard complet (AppProvider + Sidebar + pages).
 *
 * La navigation interne utilise le hachage d'URL (#/analyse, …) pour rester
 * fonctionnelle au rechargement, sans dépendance externe.
 */

import { lazy, Suspense, useEffect, useMemo, useRef, useState, type ComponentType } from "react";
import AppProvider from "./context/AppProvider";
import { ErrorBoundary } from "./components/ui";
import { AuthLayout, LoginForm, RegisterForm, type AuthResult } from "./components/auth";
import { SentimentApiClient, DEFAULT_BASE_URL } from "./api/sentimentApiClient";
import {
  SESSION_KEY,
  AUTH_TTL_SECONDS,
  buildAuthSession,
  isSessionValid,
  mapAuthErrorMessage,
  mapRegisterErrorMessage,
  readStoredBaseUrl,
  type AuthSession,
} from "./api/authSession";
import { useLocalStorage } from "./hooks/useLocalStorage";

// Sidebar chargé à la demande : prend en charge le CSS + les 10 icônes SVG
// (~30 KB sortis du chemin critique). Un fallback réservant l'espace évite
// toute reflow/CLS au swap.
const Sidebar = lazy(() => import("./components/layout/Sidebar"));

// Les pages sont chargées à la demande (code-splitting) : chaque page devient
// un bundle distinct, tiré au premier affichage. Le bundle initial reste minimal.
const HomePage = lazy(() => import("./pages/HomePage"));
const SentimentPage = lazy(() => import("./pages/SentimentPage"));
const IntentPage = lazy(() => import("./pages/IntentPage"));
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
  intention: IntentPage,
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

/**
 * Sélecteur d'URL d'API, replié par défaut : permet de se connecter à un
 * backend qui n'est pas sur localhost (paramètre volontairement discret pour
 * préserver la sobriété de la page ; valeur persistée dans localStorage).
 */
function ApiServerForm({ value, onSubmit }: { value: string; onSubmit: (url: string) => void }) {
  const [draft, setDraft] = useState(value);
  return (
    <details className="auth-server">
      <summary>Serveur API</summary>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          const url = draft.trim();
          if (url) onSubmit(url);
        }}
      >
        <input
          type="url"
          className="auth-server__input"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="http://localhost:8000"
          aria-label="URL de l'API"
        />
        <button type="submit" className="auth-server__apply">
          Appliquer
        </button>
      </form>
    </details>
  );
}

export default function App() {
  const [page, setPage] = useState(pageFromHash);
  const mainRef = useRef<HTMLElement>(null);

  // --- Session d'authentification (persistée, validée par expiration) ------
  const [session, setSession] = useLocalStorage<AuthSession | null>(
    SESSION_KEY,
    null
  );
  const authenticated = isSessionValid(session);

  // URL de l'API pour l'écran de connexion : lue depuis la config persistée
  // (le dashboard possède sa propre lecture dans l'AppProvider).
  const [apiBaseUrl, setApiBaseUrl] = useState(() =>
    readStoredBaseUrl(DEFAULT_BASE_URL)
  );

  const applyApiBaseUrl = (url: string): void => {
    try {
      window.localStorage.setItem(
        "thinktuning.apiConfig",
        JSON.stringify({ baseUrl: url })
      );
    } catch {
      /* stockage indisponible : la valeur reste valable pour la session */
    }
    setApiBaseUrl(url);
  };

  // Client « nu » pour l'écran de connexion : seule la route publique
  // POST /auth/token est appelée ici (aucune clé ni jeton transmis).
  const loginClient = useMemo(
    () => new SentimentApiClient({ baseUrl: apiBaseUrl }),
    [apiBaseUrl]
  );

  // --- Écran d'authentification : login OU inscription (bascule interne) -----
  const [authView, setAuthView] = useState<"login" | "register">("login");
  // Email du compte créé juste avant — pré-remplit le login + bannière de succès.
  const [registeredEmail, setRegisteredEmail] = useState<string | null>(null);

  const openAuthView = (view: "login" | "register"): void => {
    // Quitter la vue « post-inscription » efface le message de succès.
    if (view === "register") setRegisteredEmail(null);
    setAuthView(view);
  };

  /** Authentification RÉELLE : échange client_id/secret → JWT via l'API. */
  const handleAuthenticate = async (
    email: string,
    password: string
  ): Promise<AuthResult> => {
    try {
      // TTL 24 h demandé (plafond backend, cf. AUTH_TTL_SECONDS dans
      // api/authSession.ts) : sans cet argument, le défaut serveur (900 s)
      // ferait expirer la session dashboard toutes les 15 minutes.
      const result = await loginClient.authenticate(email, password, AUTH_TTL_SECONDS);
      return {
        email,
        token: result.token,
        tokenType: result.token_type,
        role: result.role,
        expiresIn: result.expires_in,
      };
    } catch (err) {
      // Message court + sécurisé (le backend interdit l'énumération de comptes).
      throw new Error(mapAuthErrorMessage(err), { cause: err });
    }
  };

  /** Connexion réussie : persiste la session puis ouvre le Tableau de bord. */
  const handleLoginSuccess = (result: AuthResult): void => {
    if (result.token) {
      setSession(
        buildAuthSession(result.email, result.token, {
          tokenType: result.tokenType ?? "Bearer",
          role: result.role ?? "read",
          expiresIn: result.expiresIn ?? AUTH_TTL_SECONDS,
        })
      );
    }
    window.location.hash = "/dashboard";
  };

  /**
   * Inscription RÉELLE : création du compte via POST /auth/register.
   * Le compte créé est prêt pour la connexion par email + mot de passe
   * (aucun jeton émis ici — l'échange reste une action de connexion).
   */
  const handleRegister = async (email: string, password: string): Promise<AuthResult> => {
    try {
      const result = await loginClient.register({
        email,
        password,
        accept_terms: true, // validé par RegisterForm (case CGU cochée)
      });
      return { email: result.email };
    } catch (err) {
      throw new Error(mapRegisterErrorMessage(err), { cause: err });
    }
  };

  /** Inscription réussie : retour au login, email pré-rempli + bannière. */
  const handleRegisterSuccess = (result: AuthResult): void => {
    setRegisteredEmail(result.email);
    setAuthView("login");
  };

  /** Déconnexion volontaire (Sidebar → Se déconnecter). */
  const handleLogout = (): void => {
    setSession(null);
  };

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

  // --- Non connecté : écran d'authentification (login OU inscription) ---------
  if (!authenticated) {
    const isRegister = authView === "register";
    return (
      <AuthLayout
        title={isRegister ? "Inscription" : "Connexion"}
        subtitle={isRegister ? "Créez votre compte ThinkTuning" : "Accédez à votre espace"}
        switchMessage={
          isRegister ? "Vous avez déjà un compte ?" : "Pas encore de compte ?"
        }
        createAccountLabel={isRegister ? "Se connecter" : "Créer un compte"}
        onCreateAccount={() => openAuthView(isRegister ? "login" : "register")}
      >
        {isRegister ? (
          <RegisterForm register={handleRegister} onSuccess={handleRegisterSuccess} />
        ) : (
          <LoginForm
            authenticate={handleAuthenticate}
            onSuccess={handleLoginSuccess}
            initialEmail={registeredEmail ?? ""}
            notice={
              registeredEmail
                ? `Compte créé pour ${registeredEmail}. Connectez-vous.`
                : ""
            }
          />
        )}
        <ApiServerForm value={apiBaseUrl} onSubmit={applyApiBaseUrl} />
      </AuthLayout>
    );
  }

  const ActivePage = ROUTES[page];

  return (
    <AppProvider>
      <a className="skip-link" href="#contenu">
        Aller au contenu
      </a>
      <div className="app-shell">
        <Suspense fallback={<SidebarFallback />}>
          <Sidebar page={page} onNavigate={navigate} onLogout={handleLogout} />
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
