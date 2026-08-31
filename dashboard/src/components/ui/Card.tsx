/**
 * components/ui/Card.tsx
 * ---------------------------------------------------------------------
 * Panneau (« carte ») du design system : cadre + optionnellement un en-tête
 * avec titre et actions à droite. Remplace <section className="tt-panel">.
 */

import type { HTMLAttributes, ReactNode } from "react";

export interface CardProps extends Omit<HTMLAttributes<HTMLElement>, "title"> {
  /** Titre optionnel affiché dans l'en-tête de la carte. */
  title?: ReactNode;
  /** Élément(s) à droite de l'en-tête (badge, bouton, état…). */
  extra?: ReactNode;
  children: ReactNode;
  /** Rendu en <section> (défaut) ou en <div>. */
  as?: "section" | "div";
}

export function Card({
  title,
  extra,
  children,
  as: Tag = "section",
  className,
  ...props
}: CardProps) {
  const classes = ["tt-panel"];
  if (className) classes.push(className);
  return (
    <Tag className={classes.join(" ")} {...props}>
      {(title || extra) && (
        <div className="tt-panel-head">
          {title && <h2>{title}</h2>}
          {extra}
        </div>
      )}
      {children}
    </Tag>
  );
}