/**
 * components/auth/RegisterForm.tsx
 * -----------------------------------------------------------
 * Formulaire d'inscription premium (style Linear / Vercel / Notion).
 * - Validation en temps réel : email + mot de passe (≥ 8) + confirmation
 * - Case CGU obligatoire (consentement requis créé par le backend)
 * - Loader discret dans le bouton pendant l'appel à POST /auth/register
 * - Micro-interaction : shake léger du formulaire en cas d'erreur
 * - Ids uniques préfixés `register-` (coexistence avec LoginForm)
 */

import { useRef, useState } from "react";

/** Résultat propulsé au parent après une inscription réussie. */
export interface RegisterFormResult {
  /** Email normalisé du compte créé (pré-remplit le formulaire de connexion). */
  email: string;
}

export interface RegisterFormProps {
  /** Appelée après une inscription réussie. */
  onSuccess?: (result: RegisterFormResult) => void;
  /** Inscription réelle : appel à POST /api/v1/auth/register. */
  register?: (email: string, password: string) => Promise<RegisterFormResult> | RegisterFormResult;
  /** Libellé du bouton principal. */
  submitLabel?: string;
  /** Libellé pendant l'appel réseau. */
  submittingLabel?: string;
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
export const REGISTER_MIN_PASSWORD_LENGTH = 8;

interface FieldErrors {
  email?: string;
  password?: string;
  confirm?: string;
  terms?: string;
}

function validateEmail(value: string): string | undefined {
  const email = value.trim();
  if (!email) return "Email requis";
  if (!EMAIL_RE.test(email)) return "Adresse email invalide";
  return undefined;
}

function validatePassword(value: string): string | undefined {
  if (!value) return "Mot de passe requis";
  if (value.length < REGISTER_MIN_PASSWORD_LENGTH)
    return `Au moins ${REGISTER_MIN_PASSWORD_LENGTH} caractères`;
  return undefined;
}

function validateConfirm(password: string, confirm: string): string | undefined {
  if (!confirm) return "Confirmez le mot de passe";
  if (confirm !== password) return "Les mots de passe ne correspondent pas";
  return undefined;
}

/** Inscription de démonstration (défaut hors App.tsx — latence simulée). */
async function defaultRegister(email: string, _password: string): Promise<RegisterFormResult> {
  await new Promise((resolve) => setTimeout(resolve, 700));
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
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M2.5 4.5v9M2.5 4.5a6.35 6.35 0 0 1 10 9.8A6.35 6.35 0 0 1 17.5 4.5" />
      <path d="M9.4 7.35l.7.7M13.5 8.5l.7.7M6.1 10.9l.9.9M10.6 12.5l.9.9M3.8 14.4l.8.8M8.8 16.05l.75.75" />
    </svg>
  );
}

function EyeOffIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M2.5 4.5v9M2.5 4.5a6.35 6.35 0 0 1 10 9.8A6.35 6.35 0 0 1 17.5 4.5" />
      <path d="M13.5 14.5a6.35 6.35 0 0 1 6.5 12.2A6.35 6.35 0 0 1-1.5 5.5" />
      <path d="M9.4 7.35l.7.7M13.5 8.5l.7.7M6.1 10.9l.9.9M10.6 12.5l.9.9M3.8 14.4l.8.8M8.8 16.05l.75.75" />
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

/* --- Composant principal --------------------------------------------------- */

const EMAIL_ERROR_ID = "register-email-error";
const PASSWORD_ERROR_ID = "register-password-error";
const CONFIRM_ERROR_ID = "register-confirm-error";
const TERMS_ERROR_ID = "register-terms-error";

export function RegisterForm({
  onSuccess,
  register = defaultRegister,
  submitLabel = "Créer mon compte",
  submittingLabel = "Inscription…",
}: RegisterFormProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [acceptTerms, setAcceptTerms] = useState(false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [authError, setAuthError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [shake, setShake] = useState(false);
  const [touched, setTouched] = useState<{
    email: boolean;
    password: boolean;
    confirm: boolean;
    terms: boolean;
  }>({ email: false, password: false, confirm: false, terms: false });

  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const confirmRef = useRef<HTMLInputElement>(null);
  const termsRef = useRef<HTMLInputElement>(null);

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
    if (touched.email) setErrors((prev) => ({ ...prev, email: validateEmail(value) }));
  };

  const handlePasswordChange = (value: string): void => {
    setPassword(value);
    setAuthError(null);
    if (touched.password) setErrors((prev) => ({ ...prev, password: validatePassword(value) }));
    if (touched.confirm)
      setErrors((prev) => ({ ...prev, confirm: validateConfirm(value, confirm) }));
  };

  const handleConfirmChange = (value: string): void => {
    setConfirm(value);
    setAuthError(null);
    if (touched.confirm)
      setErrors((prev) => ({ ...prev, confirm: validateConfirm(password, value) }));
  };

  const handleTermsToggle = (checked: boolean): void => {
    setAcceptTerms(checked);
    setAuthError(null);
    if (touched.terms)
      setErrors((prev) => ({
        ...prev,
        terms: checked ? undefined : "Acceptez les CGU pour continuer",
      }));
  };

  const handleEmailBlur = (): void => {
    setTouched((prev) => ({ ...prev, email: true }));
    setErrors((prev) => ({ ...prev, email: validateEmail(email) }));
  };

  const handlePasswordBlur = (): void => {
    setTouched((prev) => ({ ...prev, password: true }));
    setErrors((prev) => ({ ...prev, password: validatePassword(password) }));
  };

  const handleConfirmBlur = (): void => {
    setTouched((prev) => ({ ...prev, confirm: true }));
    setErrors((prev) => ({ ...prev, confirm: validateConfirm(password, confirm) }));
  };

  const handleTermsBlur = (): void => {
    setTouched((prev) => ({ ...prev, terms: true }));
    if (!acceptTerms)
      setErrors((prev) => ({ ...prev, terms: "Acceptez les CGU pour continuer" }));
  };

  const handleSubmit = (
    rawEmail: string,
    rawPassword: string,
    rawConfirm: string,
    terms: boolean
  ): void => {
    if (submitting) return;

    const nextErrors: FieldErrors = {
      email: validateEmail(rawEmail),
      password: validatePassword(rawPassword),
      confirm: validateConfirm(rawPassword, rawConfirm),
      terms: terms ? undefined : "Acceptez les CGU pour continuer",
    };
    setTouched({ email: true, password: true, confirm: true, terms: true });
    setErrors(nextErrors);
    setAuthError(null);

    const firstInvalid: HTMLInputElement | null = nextErrors.email
      ? emailRef.current
      : nextErrors.password
        ? passwordRef.current
        : nextErrors.confirm
          ? confirmRef.current
          : nextErrors.terms
            ? termsRef.current
            : null;
    if (firstInvalid) {
      firstInvalid.focus();
      triggerShake();
      return;
    }

    setSubmitting(true);
    void Promise.resolve()
      .then(() => register(rawEmail.trim().toLowerCase(), rawPassword))
      .then((result) => {
        onSuccess?.(result);
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : "Inscription impossible";
        setAuthError(message);
        triggerShake();
      })
      .finally(() => {
        setSubmitting(false);
      });
  };

  const emailError = errors.email;
  const passwordError = errors.password;
  const confirmError = errors.confirm;
  const termsError = errors.terms;

return (
    <form
      className={shake ? "auth-form auth-field--shake" : "auth-form"}
      noValidate
      aria-busy={submitting}
      onSubmit={(event) => {
        event.preventDefault();
        handleSubmit(email, password, confirm, acceptTerms);
      }}
      onAnimationEnd={() => setShake(false)}
    >
      {/* Bannière d'erreur au niveau du formulaire (échec d'inscription). */}
      {authError && (
        <p className="auth-form__error" role="alert">
          <AlertIcon />
          {authError}
        </p>
      )}

      <div className="auth-field">
        <label className="auth-field__label" htmlFor="register-email">
          Email
        </label>
        <div className="auth-field__wrap">
          <EmailIcon />
          <input
            ref={emailRef}
            id="register-email"
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
        <label className="auth-field__label" htmlFor="register-password">
          Mot de passe
        </label>
        <div className="auth-field__wrap">
          <LockIcon />
          <input
            ref={passwordRef}
            id="register-password"
            className="auth-field__input"
            type={showPassword ? "text" : "password"}
            name="new-password"
            autoComplete="new-password"
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

<div className="auth-field">
        <label className="auth-field__label" htmlFor="register-confirm">
          Confirmez le mot de passe
        </label>
        <div className="auth-field__wrap">
          <LockIcon />
          <input
            ref={confirmRef}
            id="register-confirm"
            className="auth-field__input"
            type={showConfirm ? "text" : "password"}
            name="confirm-password"
            autoComplete="new-password"
            placeholder="••••••••"
            value={confirm}
            disabled={submitting}
            aria-invalid={Boolean(confirmError)}
            aria-describedby={confirmError ? CONFIRM_ERROR_ID : undefined}
            onChange={(event) => handleConfirmChange(event.target.value)}
            onBlur={handleConfirmBlur}
          />
          <button
            type="button"
            className="auth-field__eye"
            onClick={() => setShowConfirm((visible) => !visible)}
            aria-label={showConfirm ? "Masquer la confirmation" : "Afficher la confirmation"}
          >
            {showConfirm ? <EyeOffIcon /> : <EyeIcon />}
          </button>
        </div>
        <p id={CONFIRM_ERROR_ID} className="auth-field__error" hidden={!confirmError}>
          {confirmError}
        </p>
      </div>

      {/* Consentement CGU : bloquant au submit, message d'erreur dédié. */}
      <div className="auth-field">
        <label className="auth-terms" htmlFor="register-terms">
          <input
            ref={termsRef}
            id="register-terms"
            type="checkbox"
            name="accept_terms"
            checked={acceptTerms}
            disabled={submitting}
            onChange={(event) => handleTermsToggle(event.target.checked)}
            onBlur={handleTermsBlur}
          />
          <span>
            J'accepte les{" "}
            <a href="#" onClick={(event) => event.preventDefault()}>
              conditions d'utilisation
            </a>{" "}
            et la politique de confidentialité
          </span>
        </label>
        <p id={TERMS_ERROR_ID} className="auth-field__error" hidden={!termsError}>
          {termsError}
        </p>
      </div>

      {/* Bouton principal avec loader discret pendant l'inscription. */}
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
    </form>
  );
}