/**
 * components/ui/EmptyState.tsx
 * ---------------------------------------------------------------------
 * État vide réutilisable (« Aucune donnée ») pour remplacer les paragraphes
 * .tt-hint répétés quand une liste est vide.
 */

import type { ReactNode } from "react";

export interface EmptyStateProps {
  /** Message principal. */
  children: ReactNode;
  /** Message secondaire optionnel (format plus discret). */
  hint?: ReactNode;
}

export function EmptyState({ children, hint }: EmptyStateProps) {
  return (
    <p className="tt-empty">
      <span>{children}</span>
      {hint && <span className="tt-empty__hint">{hint}</span>}
    </p>
  );
}