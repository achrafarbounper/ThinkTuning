/**
 * components/ui/Spinner.tsx
 * ---------------------------------------------------------------------
 * Indicateur de chargement compact et accessible (aria-label en français).
 */

import type { SVGProps } from "react";

export interface SpinnerProps extends SVGProps<SVGSVGElement> {
  /** Taille du spinner en pixels (carré). */
  size?: number;
  /** Libellé lu par les technologies d'assistance. */
  label?: string;
}

export function Spinner({
  size = 18,
  label = "Chargement…",
  ...props
}: SpinnerProps) {
  return (
    <svg
      className="tt-spinner"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      role="status"
      aria-label={label}
      {...props}
    >
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" opacity="0.25" />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}