/**
 * useApp.ts — Hook d'accès à l'état global du dashboard.
 * Doit être utilisé sous <AppProvider>.
 */

import { useContext } from "react";
import { AppContext, type AppState } from "./appContext";

export function useApp(): AppState {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp doit être utilisé dans <AppProvider>");
  return ctx;
}
