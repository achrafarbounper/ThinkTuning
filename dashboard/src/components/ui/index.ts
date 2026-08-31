/**
 * components/ui/index.ts
 * ---------------------------------------------------------------------
 * Point d'entrée unique des composants UI réutilisables (design system
 * maison). Importer depuis un seul endroit : `import { Card, Button } from
 * "@/components/ui"`.
 */

export { Button } from "./Button";
export type { ButtonProps, ButtonVariant } from "./Button";
export { Badge } from "./Badge";
export type { BadgeProps, BadgeTone } from "./Badge";
export { Card } from "./Card";
export type { CardProps } from "./Card";
export { Spinner } from "./Spinner";
export type { SpinnerProps } from "./Spinner";
export { EmptyState } from "./EmptyState";
export type { EmptyStateProps } from "./EmptyState";
export { ErrorBoundary } from "./ErrorBoundary";
export type { ErrorBoundaryProps } from "./ErrorBoundary";