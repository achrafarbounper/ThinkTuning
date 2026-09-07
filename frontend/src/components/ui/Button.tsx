/**
 * components/ui/Button.tsx
 * ---------------------------------------------------------------------
 * Bouton réutilisable adossé au design system existant (.tt-btn).
 * Remplace les balises <button className="tt-btn …"> répétées dans les pages.
 */

import type { ButtonHTMLAttributes, ReactNode } from "react";

export type ButtonVariant = "primary" | "ghost" | "danger";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  children: ReactNode;
}

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  primary: "tt-btn-primary",
  ghost: "tt-btn-ghost",
  danger: "tt-btn-danger",
};

export function Button({
  variant = "ghost",
  className,
  children,
  ...props
}: ButtonProps) {
  const classes = [`tt-btn`, VARIANT_CLASS[variant]];
  if (className) classes.push(className);
  return (
    <button type="button" className={classes.join(" ")} {...props}>
      {children}
    </button>
  );
}