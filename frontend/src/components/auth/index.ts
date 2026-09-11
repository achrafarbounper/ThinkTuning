/**
 * components/auth/index.ts
 * ---------------------------------------------------------------------
 * Point d'entrée unique des composants d'authentification premium.
 * Importer depuis un seul endroit :
 *   import { AuthLayout, LoginForm } from "@/components/auth";
 */

export { AuthLayout } from "./AuthLayout";
export type { AuthLayoutProps, AuthTheme } from "./AuthLayout";
export { LoginForm } from "./LoginForm";
export type { AuthResult, LoginFormProps } from "./LoginForm";