/**
 * components/ui/ErrorBoundary.tsx
 * ---------------------------------------------------------------------
 * Filet de sécurité pour les erreurs de rendu (souvent dues au chargement
 * asynchrone d'une page lazy). Empêche le crash silencieux de toute l'app :
 * affiche un message + un bouton « Réessayer ».
 *
 * Notes :
 *  - classe (Component) — le pattern error boundary l'exige en React ;
 *  - le reset du state permet de relancer le rendu des enfants.
 */

import { Component, type ReactNode } from "react";

export interface ErrorBoundaryProps {
  children: ReactNode;
  /** Libellé du message d'erreur (défaut français). */
  message?: string;
  /** Texte du bouton de relance. */
  retryLabel?: string;
}

interface ErrorBoundaryState {
  hasError: boolean;
  message: string;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { hasError: false, message: "" };

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return {
      hasError: true,
      message: error instanceof Error ? error.message : String(error),
    };
  }

  private handleRetry = () => {
    this.setState({ hasError: false, message: "" });
  };

  render() {
    if (this.state.hasError) {
      return (
        <div className="tt-error-boundary" role="alert">
          <p className="tt-error-boundary__title">
            {this.props.message ?? "Une erreur est survenue."}
          </p>
          {this.state.message && (
            <pre className="tt-error-boundary__detail">{this.state.message}</pre>
          )}
          <button
            type="button"
            className="tt-btn tt-btn-ghost"
            onClick={this.handleRetry}
          >
            {this.props.retryLabel ?? "Réessayer"}
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}