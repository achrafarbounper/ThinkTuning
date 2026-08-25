/**
 * Contexte React partagé par toutes les pages du dashboard.
 *
 * Fichier volontairement séparé du Provider (AppContext.jsx) et du hook
 * (useApp.js) pour satisfaire la règle react-refresh/only-export-components :
 * ce module n'exporte aucun composant.
 */

import { createContext } from "react";

export const AppContext = createContext(null);
