/**
 * components/auth/LoginForm.tsx
 * -----------------------------------------------------------
 * Formulaire de connexion premium (style Linear / Vercel / Notion).
 * - Validation en temps réel (email + mot de passe requis)
 * - Messages d'erreur courts : « Email introuvable », « Mot de passe incorrect »
 * - Loader discret dans le bouton pendant la connexion
 * - Micro-interaction : shake léger du formulaire en cas d'erreur
 * - Icônes email + lock, œil pour afficher/masquer le mot de passe
 * - Connexion sociale Google / Apple (marques officielles)
 */

import { useRef, useState } from "react";

export interface AuthResult {
  email: string;
  /** Jeton JWT émis par le backend (renseigné par une authentification réelle). */
  token?: string;
  tokenType?: string;
  role?: string;
  /** Durée de vie du jeton en secondes. */
  expiresIn?: number;
}

export type SocialProvider = "google" | "apple";

export interface LoginFormProps {
  /** Appelée après une connexion réussie. */
  onSuccess?: (result: AuthResult) => void;
  /** Authentification réelle (démo : demo@thinktuning.app / demo1234). */
  authenticate?: (email: string, password: string) => Promise<AuthResult> | AuthResult;
  /** Callback « Mot de passe oublié ? ». */
  onForgotPassword?: () => void;
  /** Callback connexion sociale. */
  onSocial?: (provider: SocialProvider) => void;
  /** Email pré-rempli (après une inscription réussie). */
  initialEmail?: string;
  /** Message d'information affiché en tête du formulaire (ex. succès d'inscription). */
  notice?: string;
  /** Libellé du bouton principal. */
  submitLabel?: string;
  /** Libellé pendant la connexion. */
  submittingLabel?: string;
}

const DEMO_EMAIL = "demo@thinktuning.app";
const DEMO_PASSWORD = "demo1234";
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

interface FieldErrors {
  email?: string;
  password?: string;
}

function validateEmail(value: string): string | undefined {
  const email = value.trim();
  if (!email) return "Email requis";
  if (!EMAIL_RE.test(email)) return "Adresse email invalide";
  return undefined;
}

function validatePassword(value: string): string | undefined {
  if (!value) return "Mot de passe requis";
  return undefined;
}

/** Authentification de démonstration : latence simulée + échec explicite. */
async function defaultAuthenticate(email: string, password: string): Promise<AuthResult> {
  await new Promise((resolve) => setTimeout(resolve, 900));
  if (email.trim().toLowerCase() !== DEMO_EMAIL) {
    throw new Error("Email introuvable");
  }
  if (password !== DEMO_PASSWORD) {
    throw new Error("Mot de passe incorrect");
  }
  return { email: email.trim().toLowerCase() };
}

/* --- Icônes ----------------------------------------------------------------- */

function EmailIcon() {
  return (
    <svg
      className="auth-field__icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="2.5" y="5" width="19" height="14" rx="3" />
      <path d="m3.5 7 8.5 6 8.5-6" />
    </svg>
  );
}

function LockIcon() {
  return (
    <svg
      className="auth-field__icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="4.5" y="10.5" width="15" height="10" rx="2.5" />
      <path d="M8 10.5V7.5a4 4 0 0 1 8 0v3" />
    </svg>
  );
}

function EyeIcon() {
  return (
    <svg
      width="17"
      height="17"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function EyeOffIcon() {
  return (
    <svg
      width="17"
      height="17"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94" />
      <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19" />
      <line x1="1" y1="1" x2="23" y2="23" />
    </svg>
  );
}

function AlertIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="8" x2="12" y2="12" />
      <line x1="12" y1="16" x2="12.01" y2="16" />
    </svg>
  );
}

/** Cocher verte (bannière d'information — ex. compte créé). */
function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 6.5c3 0 5.5 5.5 5.5-5.5L16 18" />
      <path d="M16 8c-3.5 0-5.5 5.5-5.5 5.5L4 18" />
    </svg>
  );
}

/** Marque Google officielle (simple-icons, bleu #4285F4). */
function GoogleIcon() {
  return (
    <svg className="auth-social__icon" viewBox="0 0 24 24" fill="#4285F4" aria-hidden="true">
      <path d="M12.48 10.92v3.28h7.84c-.24 1.84-.853 3.187-1.787 4.133-1.147 1.147-2.933 2.4-6.053 2.4-4.827 0-8.6-3.893-8.6-8.72s3.773-8.72 8.6-8.72c2.6 0 4.507 1.027 5.907 2.347l2.307-2.307C18.747 1.44 16.133 0 12.48 0 5.867 0 .307 5.387.307 12s5.56 12 12.173 12c3.573 0 6.267-1.173 8.373-3.36 2.16-2.16 2.84-5.213 2.84-7.667 0-.76-.053-1.467-.173-2.053H12.48z" />
    </svg>
  );
}

/** Marque Apple officielle (simple-icons, hérite de la couleur du texte). */
function AppleIcon() {
  return (
    <svg className="auth-social__icon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M12.152 6.896c-.948 0-2.415-1.078-3.96-1.04-2.04.027-3.91 1.183-4.961 3.014-2.117 3.675-.546 9.103 1.519 12.09 1.013 1.454 2.208 3.09 3.792 3.039 1.52-.065 2.09-.987 3.935-.987 1.831 0 2.35.987 3.96.948 1.637-.026 2.676-1.48 3.676-2.948 1.156-1.688 1.636-3.325 1.662-3.415-.039-.013-3.182-1.221-3.22-4.857-.026-3.04 2.48-4.494 2.597-4.559-1.429-2.09-3.623-2.324-4.39-2.376-2-.156-3.675 1.09-4.61 1.09zM15.53 3.83c.843-1.012 1.4-2.427 1.245-3.83-1.207.052-2.662.805-3.532 1.818-.78.896-1.454 2.338-1.273 3.714 1.338.104 2.715-.688 3.559-1.701" />
    </svg>
  );
}

/* --- Composant principal --------------------------------------------------- */

const EMAIL_ERROR_ID = "auth-email-error";
const PASSWORD_ERROR_ID = "auth-password-error";

export function LoginForm({
  onSuccess,
  authenticate = defaultAuthenticate,
  onForgotPassword,
  onSocial,
  initialEmail = "",
  notice = "",
  submitLabel = "Se connecter",
  submittingLabel = "Connexion…",
}: LoginFormProps) {
  const [email, setEmail] = useState(initialEmail);
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<FieldErrors>({});
  const [authError, setAuthError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [shake, setShake] = useState(false);
  const [touched, setTouched] = useState<{ email: boolean; password: boolean }>({
    email: false,
    password: false,
  });

  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  /** Redémarre proprement la secousse (double requestAnimationFrame). */
  const triggerShake = (): void => {
    setShake(false);
    void window.requestAnimationFrame(() => {
      void window.requestAnimationFrame(() => setShake(true));
    });
  };

  const handleEmailChange = (value: string): void => {
    setEmail(value);
    setAuthError(null);
    if (touched.email) {
      setErrors((prev) => ({ ...prev, email: validateEmail(value) }));
    }
  };

  const handlePasswordChange = (value: string): void => {
    setPassword(value);
    setAuthError(null);
    if (touched.password) {
      setErrors((prev) => ({ ...prev, password: validatePassword(value) }));
    }
  };

  const handleEmailBlur = (): void => {
    setTouched((prev) => ({ ...prev, email: true }));
    setErrors((prev) => ({ ...prev, email: validateEmail(email) }));
  };

  const handlePasswordBlur = (): void => {
    setTouched((prev) => ({ ...prev, password: true }));
    setErrors((prev) => ({ ...prev, password: validatePassword(password) }));
  };

  const handleSubmit = (rawEmail: string, rawPassword: string): void => {
    if (submitting) return;

    const nextErrors: FieldErrors = {
      email: validateEmail(rawEmail),
      password: validatePassword(rawPassword),
    };
    setTouched({ email: true, password: true });
    setErrors(nextErrors);
    setAuthError(null);

    if (nextErrors.email || nextErrors.password) {
      (nextErrors.email ? emailRef : passwordRef).current?.focus();
      triggerShake();
      return;
    }

    setSubmitting(true);
    void Promise.resolve()
      .then(() => authenticate(rawEmail.trim().toLowerCase(), rawPassword))
      .then((result) => {
        onSuccess?.(result);
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : "Connexion impossible";
        setAuthError(message);
        triggerShake();
      })
      .finally(() => {
        setSubmitting(false);
      });
  };

  const emailError = errors.email;
  const passwordError = errors.password;

  return (
    <form
      className={shake ? "auth-form auth-field--shake" : "auth-form"}
      noValidate
      aria-busy={submitting}
      onSubmit={(event) => {
        event.preventDefault();
        handleSubmit(email, password);
      }}
      onAnimationEnd={() => setShake(false)}
    >
      {/* Bannière d'information (ex. succès d'inscription — compte prêt). */}
      {notice && (
        <p className="auth-form__notice" role="status">
          <CheckIcon />
          {notice}
        </p>
      )}

      {/* Bannière d'erreur au niveau du formulaire (échec d'authentification). */}
      {authError && (
        <p className="auth-form__error" role="alert">
          <AlertIcon />
          {authError}
        </p>
      )}

      <div className="auth-field">
        <label className="auth-field__label" htmlFor="auth-email">
          Email
        </label>
        <div className="auth-field__wrap">
          <EmailIcon />
          <input
            ref={emailRef}
            id="auth-email"
            className="auth-field__input"
            type="email"
            name="email"
            autoComplete="email"
            placeholder="vous@exemple.com"
            value={email}
            disabled={submitting}
            aria-invalid={Boolean(emailError)}
            aria-describedby={emailError ? EMAIL_ERROR_ID : undefined}
            onChange={(event) => handleEmailChange(event.target.value)}
            onBlur={handleEmailBlur}
          />
        </div>
        <p id={EMAIL_ERROR_ID} className="auth-field__error" hidden={!emailError}>
          {emailError}
        </p>
      </div>

      <div className="auth-field">
        <label className="auth-field__label" htmlFor="auth-password">
          Mot de passe
        </label>
        <div className="auth-field__wrap">
          <LockIcon />
          <input
            ref={passwordRef}
            id="auth-password"
            className="auth-field__input"
            type={showPassword ? "text" : "password"}
            name="password"
            autoComplete="current-password"
            placeholder="••••••••"
            value={password}
            disabled={submitting}
            aria-invalid={Boolean(passwordError)}
            aria-describedby={passwordError ? PASSWORD_ERROR_ID : undefined}
            onChange={(event) => handlePasswordChange(event.target.value)}
            onBlur={handlePasswordBlur}
          />
          <button
            type="button"
            className="auth-field__eye"
            onClick={() => setShowPassword((visible) => !visible)}
            aria-label={showPassword ? "Masquer le mot de passe" : "Afficher le mot de passe"}
          >
            {showPassword ? <EyeOffIcon /> : <EyeIcon />}
          </button>
        </div>
        <p id={PASSWORD_ERROR_ID} className="auth-field__error" hidden={!passwordError}>
          {passwordError}
        </p>
      </div>

      {/* Bouton principal avec loader discret pendant la connexion. */}
      <button type="submit" className="auth-submit" disabled={submitting}>
        {submitting ? (
          <>
            <svg className="auth-submit__spinner" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" opacity="0.25" />
              <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
            </svg>
            <span className="auth-submit__loader-text">{submittingLabel}</span>
          </>
        ) : (
          submitLabel
        )}
      </button>

      {/* Lien secondaire : mot de passe oublié. */}
      <div className="auth-links">
        {onForgotPassword ? (
          <button type="button" className="auth-links__btn" onClick={onForgotPassword}>
            Mot de passe oublié ?
          </button>
        ) : (
          <a href="#" onClick={(event) => event.preventDefault()}>
            Mot de passe oublié ?
          </a>
        )}
      </div>

      {/* Connexion sociale : Google + Apple. */}
      <div className="auth-divider" aria-hidden="true">
        ou continuer avec
      </div>

      <div className="auth-social">
        <button type="button" className="auth-social__btn" onClick={() => onSocial?.("google")}>
          <GoogleIcon />
          Continuer avec Google
        </button>
        <button type="button" className="auth-social__btn" onClick={() => onSocial?.("apple")}>
          <AppleIcon />
          Continuer avec Apple
        </button>
      </div>
    </form>
  );
}