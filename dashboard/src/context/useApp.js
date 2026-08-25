/** Hook d'accès à l'état global du dashboard (doit être sous <AppProvider>). */

import { useContext } from "react";
import { AppContext } from "./appContext";

export function useApp() {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp doit être utilisé dans <AppProvider>");
  return ctx;
}
