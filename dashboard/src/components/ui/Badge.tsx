/**
 * components/ui/Badge.tsx
 * ---------------------------------------------------------------------
 * Badge d'état adossé au design system (.tt-badge + nuances). Sert aussi de
 * brique réutilisable pour les classes de sentiment (ton positive/neutral/negative).
 */

import type { HTMLAttributes, ReactNode } from "react";

export type BadgeTone = "positive" | "neutral" | "negative" | "plain";

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
  children: ReactNode;
}

export function Badge({
  tone = "plain",
  className,
  children,
  ...props
}: BadgeProps) {
  const classes = ["tt-badge"];
  if (tone !== "plain") classes.push(`tt-badge-${tone}`);
  if (className) classes.push(className);
  return (
    <span className={classes.join(" ")} {...props}>
      {children}
    </span>
  );
}