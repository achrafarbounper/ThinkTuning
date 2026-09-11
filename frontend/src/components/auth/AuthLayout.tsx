/**
 * components/auth/AuthLayout.tsx
 * ---------------------------------------------------------------------
 * Page d'authentification premium (style Linear / Vercel / Notion) :
 * carte centrée verticalement, fond clair/sombre, toggle du thème fixé
 * en haut à droite et footer minimal (langue + mentions légales).
 *
 * Usage :
 *   <AuthLayout>
 *     <LoginForm onSuccess={handleSuccess} />
 *   </AuthLayout>
 */

import { useState, type ReactNode } from "react";
import "./auth.css";

export type AuthTheme = "light" | "dark";

export interface AuthLayoutProps {
  /** Contenu de la carte : typiquement <LoginForm />. */
  children: ReactNode;
  /** Monogramme de la marque (44px, dégradé brand). */
  brand?: string;
  /** Thème initial de la page. */
  defaultTheme?: AuthTheme;
  /** Langue active affichée dans le footer. */
  lang?: "fr" | "en";
  /** Message de l'encart hors-carte (ex. « Pas de compte ? »). */
  switchMessage?: string;
  /** Libellé du lien de création de compte. */
  createAccountLabel?: string;
  /** Callback « Créer un compte ». */
  onCreateAccount?: () => void;
}

/** Icône soleil (toggle du thème, mode clair actif). */
function SunIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="4.4" />
      <path d="M12 4.5v2.6M10.8 5.8l1.2 1.2M9.1 7.4l1.2 1.1M7.4 9.1l1.1 1.2M5.8 10.8l1.2 1.2M4.5 12v2.6M5.8 13.2l1.2 1.2M7.4 14.6l1.1 1.1M9.1 16.1l1.2 1.1M10.8 17.4l1.2 1.2M12 19.5v2.6M13.2 18.2l1.2 1.2M14.6 16.6l1.1 1.1M16.1 15.1l1.2 1.1M17.4 13.6l1.2 1.2M19.5 12v2.6M18.2 10.8l1.2 1.2M16.6 9.4l1.1 1.1M15.1 7.9l1.2 1.1M13.6 6.4l1.2 1.2" />
    </svg>
  );
}

/** Icône lune (toggle du thème, mode sombre actif). */
function MoonIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <path d="M21 12.7A9 9 0 0 0-9-9" />
    </svg>
  );
}

const TITLE_ID = "auth-title";

export function AuthLayout({
  children,
  brand = "TT",
  defaultTheme = "light",
  lang = "fr",
  switchMessage = "Pas encore de compte ?",
  createAccountLabel = "Créer un compte",
  onCreateAccount,
}: AuthLayoutProps) {
  const [theme, setTheme] = useState<AuthTheme>(defaultTheme);

  const toggleTheme = (): void => {
    setTheme(theme === "light" ? "dark" : "light");
  };

  return (
    <div className="auth-page" data-theme={theme}>
      {/* Toggle clair / sombre, hors du flux de la carte. */}
      <button
        type="button"
        className="auth-theme-toggle"
        onClick={toggleTheme}
        aria-label={theme === "light" ? "Activer le mode sombre" : "Activer le mode clair"}
      >
        {theme === "light" ? <MoonIcon /> : <SunIcon />}
      </button>

      <section className="auth-card" aria-labelledby={TITLE_ID}>
        <header className="auth-card__head">
          <div className="auth-card__brand">
            <span className="auth-card__brand-mark" aria-hidden="true">
              {brand}
            </span>
          </div>
          <h1 id={TITLE_ID} className="auth-card__title">
            Connexion
          </h1>
          <p className="auth-card__subtitle">Accédez à votre espace</p>
        </header>

        {children}
      </section>

      {/* Encart hors-carte : création de compte. */}
      <p className="auth-switch">
        {switchMessage}{" "}
        {onCreateAccount ? (
          <button type="button" className="auth-switch__link" onClick={onCreateAccount}>
            {createAccountLabel}
          </button>
        ) : (
          <a className="auth-switch__link" href={createAccountLabel === "Créer un compte" ? "/register" : "#"}>
            {createAccountLabel}
          </a>
        )}
      </p>

      {/* Footer minimal : langue + mentions légales. */}
      <footer className="auth-footer">
        <button type="button" className="auth-footer__lang">
          {lang === "fr" ? "Français" : "English"}
        </button>
        <a href="#" onClick={(event) => event.preventDefault()}>
          Mentions légales
        </a>
        <a href="#" onClick={(event) => event.preventDefault()}>
          Confidentialité
        </a>
        <a href="#" onClick={(event) => event.preventDefault()}>
          CGU
        </a>
      </footer>
    </div>
  );
}