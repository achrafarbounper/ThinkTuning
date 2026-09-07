/**
 * appContext.ts
 * ---------------------------------------------------------------------
 * Contexte React partagé par toutes les pages du dashboard.
 *
 * Fichier volontairement séparé du Provider (AppProvider.tsx) et du hook
 * (useApp.ts) pour satisfaire la règle react-refresh/only-export-components :
 * ce module n'exporte aucun composant.
 */

import { createContext } from "react";
import type { SentimentApiClient } from "../api/sentimentApiClient";
import type { AgentSettings } from "../api/agentSettings";
import type { ApiHealth, ModelVersion, PredictionResult } from "../api/sentimentApiClient";

/** Entrée du journal d'activité. */
export interface ActivityLog {
  id: number;
  type: "info" | "success" | "error";
  text: string;
  ts: number;
}

/** Configuration de connexion à l'API. */
export interface ApiConnectionConfig {
  baseUrl: string;
  apiKey: string;
}

/** Valeur exposée par le contexte global. */
export interface AppState {
  /** Client API instancié une seule fois (mémoïsé sur `config`). */
  client: SentimentApiClient;
  config: ApiConnectionConfig;
  setConfig: (config: ApiConnectionConfig) => void;
  saveConfig: (config: ApiConnectionConfig) => void;
  // --- Santé & modèles ---------------------------------------------------
  health: ApiHealth | null;
  healthError: string | null;
  models: ModelVersion[];
  modelsError: string | null;
  refreshModels: () => Promise<void>;
  activeModel: string;
  setActiveModel: (model: string) => void;
  // --- Historique des prédictions ----------------------------------------
  predictionsHistory: PredictionResult[];
  addToHistory: (preds: PredictionResult[]) => void;
  clearHistory: () => void;
  maxHistorySize: number;
  setMaxHistorySize: (size: number | string) => void;
  // --- Assistant IA --------------------------------------------------------
  agentSettings: AgentSettings;
  /**
   * Persiste les paramètres agent (état + localStorage). Accepte un updater
   * fonctionnel pour fusionner sans closure obsolète — nécessaire car
   * l'API ne renvoie JAMAIS les clés secrètes (openrouter_api_key…) :
   * la valeur locale doit être préservée sans relire le stockage.
   */
  persistAgentSettings: (
    settings: AgentSettings | ((prev: AgentSettings) => AgentSettings)
  ) => void;
  updateAgentSettings: (updates: Partial<AgentSettings>) => Promise<void>;
  testAgentConnection: (testParams: Partial<AgentSettings>) => Promise<unknown>;
  agentLoading: boolean;
  agentError: string | null;
  setAgentError: (error: string | null) => void;
  // --- Journal d'activité --------------------------------------------------
  logs: ActivityLog[];
  pushLog: (type: ActivityLog["type"], text: string) => void;
}

export const AppContext = createContext<AppState | null>(null);
