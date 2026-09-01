/**
 * Provider global du dashboard : configuration API, santé, modèles,
 * historique des prédictions, journal d'activité et paramètres de l'assistant IA.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  SentimentApiClient,
  agentSettingsPayload,
  normalizeAgentSettings,
  DEFAULT_BASE_URL,
} from "../api/sentimentApiClient";
import type { AgentSettings } from "../api/agentSettings";
import type { ApiHealth, ModelVersion, PredictionResult } from "../api/sentimentApiClient";
import { useLocalStorage } from "../hooks/useLocalStorage";
import { usePolling } from "../hooks/usePolling";
import { AppContext, type ActivityLog, type ApiConnectionConfig, type AppState } from "./appContext";

const DEFAULT_MAX_HISTORY = 20;
const HEALTH_POLL_MS = 8000;
const MODELS_POLL_MS = 15000;
// Différage du premier poll hors du chemin critique : le /health (450–502 ms)
// et le listModels ne bloquent plus le rendu initial ni le LCP.
const HEALTH_FIRST_DELAY_MS = 2000;
const MODELS_FIRST_DELAY_MS = 2500;
const AGENT_DEFAULTS: AgentSettings = {
  provider: "ollama",
  model: "",
  ollamaUrl: "",
  openrouterUrl: "https://openrouter.ai/api/v1",
  openrouterApiKey: "",
  timeoutSeconds: 60,
  contextLength: 512,
  temperature: 0.2,
};

function loadStoredConfig(): ApiConnectionConfig {
  try {
    const raw = window.localStorage.getItem("thinktuning.apiConfig");
    if (!raw) return { baseUrl: DEFAULT_BASE_URL, apiKey: "" };
    const parsed = JSON.parse(raw) as Partial<ApiConnectionConfig>;
    return { baseUrl: parsed.baseUrl || DEFAULT_BASE_URL, apiKey: parsed.apiKey || "" };
  } catch {
    return { baseUrl: DEFAULT_BASE_URL, apiKey: "" };
  }
}

function loadAgentSettings(): AgentSettings {
  try {
    const raw = window.localStorage.getItem("thinktuning.agentSettings");
    if (!raw) return { ...AGENT_DEFAULTS };
    return { ...AGENT_DEFAULTS, ...(JSON.parse(raw) as Partial<AgentSettings>) };
  } catch {
    return { ...AGENT_DEFAULTS };
  }
}

function saveAgentSettings(settings: AgentSettings): void {
  try {
    window.localStorage.setItem("thinktuning.agentSettings", JSON.stringify(settings));
  } catch {
    /* stockage indisponible */
  }
}

function loadStoredMaxHistorySize(): number {
  try {
    const raw = window.localStorage.getItem("thinktuning.maxHistorySize");
    const size = parseInt(raw ?? "", 10);
    return Number.isNaN(size) ? DEFAULT_MAX_HISTORY : Math.max(1, size);
  } catch {
    return DEFAULT_MAX_HISTORY;
  }
}
export default function AppProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<ApiConnectionConfig>(loadStoredConfig);
  const [health, setHealth] = useState<ApiHealth | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [models, setModels] = useState<ModelVersion[]>([]);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [activeModel, setActiveModel] = useState("");
  // Historique persisté via le hook réutilisable (fini les try/catch dupliqués).
  const [predictionsHistory, setPredictionsHistory] = useLocalStorage<PredictionResult[]>(
    "thinktuning.predictionsHistory",
    []
  );
  const [maxHistorySizeState, setMaxHistorySizeState] = useState(loadStoredMaxHistorySize);
  const [agentSettings, setAgentSettings] = useState<AgentSettings>(loadAgentSettings);
  const [agentLoading, setAgentLoading] = useState(false);
  const [agentError, setAgentError] = useState<string | null>(null);
  const [logs, setLogs] = useState<ActivityLog[]>([]);
  const logIdRef = useRef(0);
  const client = useMemo(() => new SentimentApiClient(config), [config]);

  const pushLog = useCallback((type: ActivityLog["type"], text: string) => {
    logIdRef.current += 1;
    setLogs((prev) =>
      [{ id: logIdRef.current, type, text, ts: Date.now() }, ...prev].slice(0, 25)
    );
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem("thinktuning.apiConfig", JSON.stringify(config));
    } catch {
      /* stockage indisponible */
    }
  }, [config]);

  // --- Polling santé & modèles (pause auto quand l'onglet est masqué) --------
  // Le premier tick est différé (initialDelayMs) : /health et listModels sont
  // sortis du chemin critique. Le rendu initial et le LCP ne bloquent plus sur
  // ces requêtes non essentielles (auparavant immédiates = ~450–502 ms).
  const pollHealth = useCallback(async () => {
    try {
      const result = await client.getHealth();
      setHealth(result);
      setHealthError(null);
    } catch (err) {
      setHealth(null);
      setHealthError(err instanceof Error ? err.message : String(err));
    }
  }, [client]);

  usePolling({
    intervalMs: HEALTH_POLL_MS,
    immediate: true,
    initialDelayMs: HEALTH_FIRST_DELAY_MS,
    tick: pollHealth,
  });

  const refreshModels = useCallback(async () => {
    if (!config.apiKey) return;
    try {
      const list = await client.listModels();
      setModels(list ?? []);
      setModelsError(null);
    } catch (err) {
      setModelsError(err instanceof Error ? err.message : String(err));
    }
  }, [client, config.apiKey]);

  // Le polling des modèles est mis en pause sans clé API (flot d'appels inutile)
  // et différé pour rester hors du chemin critique.
  usePolling({
    intervalMs: MODELS_POLL_MS,
    immediate: true,
    initialDelayMs: MODELS_FIRST_DELAY_MS,
    enabled: Boolean(config.apiKey),
    tick: refreshModels,
  });

  const persistAgentSettings = useCallback((settings: AgentSettings) => {
    saveAgentSettings(settings);
    setAgentSettings(settings);
  }, []);

  // Réutilise le client memoïsé (même config) au lieu de le réinstancier.
  const updateAgentSettings = useCallback(
    async (updates: Partial<AgentSettings>) => {
      setAgentLoading(true);
      try {
        const resp = await client._request<{ settings?: Record<string, unknown> }>(
          "/api/agent/settings",
          { method: "PUT", body: agentSettingsPayload(updates) }
        );
        if (resp) {
          const normalized = normalizeAgentSettings(resp.settings);
          // La clé n'est jamais renvoyée par l'API : on conserve celle saisie.
          persistAgentSettings({
            ...normalized,
            openrouterApiKey: updates.openrouterApiKey || normalized.openrouterApiKey,
          });
        }
        setAgentError(null);
        pushLog("success", "Paramètres assistant IA enregistrés.");
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        setAgentError(message);
        pushLog("error", "Échec enregistrement IA: " + message);
        throw err;
      } finally {
        setAgentLoading(false);
      }
    },
    [client, persistAgentSettings, pushLog]
  );

  const testAgentConnection = useCallback(
    async (testParams: Partial<AgentSettings>) => {
      setAgentLoading(true);
      try {
        return await client._request("/api/agent/settings/test", {
          method: "POST",
          body: agentSettingsPayload(testParams),
        });
      } finally {
        setAgentLoading(false);
      }
    },
    [client]
  );

  // Charge les paramètres de l'agent au montage (si une clé API est configurée).
  useEffect(() => {
    const loadAgent = async () => {
      setAgentLoading(true);
      try {
        const res = await client._request<{ settings?: Record<string, unknown> }>(
          "/api/agent/settings",
          { method: "GET" }
        );
        if (res) {
          const normalized = normalizeAgentSettings(res.settings);
          // La clé n'est jamais renvoyée par l'API : on réutilise la valeur locale.
          const local = loadAgentSettings();
          persistAgentSettings({
            ...normalized,
            openrouterApiKey: local.openrouterApiKey || normalized.openrouterApiKey,
          });
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        setAgentError(message);
        pushLog("error", "Impossible de charger les paramètres IA: " + message);
      } finally {
        setAgentLoading(false);
      }
    };
    if (config.apiKey) void loadAgent();
  }, [client, config.apiKey, persistAgentSettings, pushLog]);

  const addToHistory = useCallback(
    (newPreds: PredictionResult[]) => {
      setPredictionsHistory((prev) =>
        [...newPreds.map((p) => ({ ...p, timestamp: Date.now() })), ...prev].slice(
          0,
          maxHistorySizeState
        )
      );
    },
    [maxHistorySizeState, setPredictionsHistory]
  );

  const clearHistory = useCallback(() => {
    setPredictionsHistory([]);
    pushLog("info", "Historique des prédictions effacé.");
  }, [pushLog, setPredictionsHistory]);

  const setMaxHistorySize = useCallback(
    (size: number | string) => {
      const parsed = Number(size);
      const newSize =
        size === "" || Number.isNaN(parsed)
          ? DEFAULT_MAX_HISTORY
          : Math.max(1, Math.min(1000, parsed));
      setMaxHistorySizeState(newSize);
      try {
        window.localStorage.setItem("thinktuning.maxHistorySize", String(newSize));
      } catch {
        /* stockage indisponible */
      }
      setPredictionsHistory((prev) => prev.slice(0, newSize));
    },
    [setPredictionsHistory]
  );

  const saveConfig = useCallback(
    (c: ApiConnectionConfig) => {
      setConfig(c);
      pushLog("info", "Configuration mise à jour → " + c.baseUrl);
    },
    [pushLog]
  );
  const value = useMemo<AppState>(
    () => ({
      client,
      config,
      setConfig,
      saveConfig,
      agentSettings,
      persistAgentSettings,
      updateAgentSettings,
      testAgentConnection,
      agentLoading,
      agentError,
      setAgentError,
      health,
      healthError,
      models,
      modelsError,
      refreshModels,
      activeModel,
      setActiveModel,
      predictionsHistory,
      addToHistory,
      clearHistory,
      maxHistorySize: maxHistorySizeState,
      setMaxHistorySize,
      logs,
      pushLog,
    }),
    [
      client,
      config,
      agentSettings,
      agentLoading,
      agentError,
      health,
      healthError,
      models,
      modelsError,
      activeModel,
      predictionsHistory,
      maxHistorySizeState,
      logs,
      pushLog,
      addToHistory,
      clearHistory,
      persistAgentSettings,
      refreshModels,
      saveConfig,
      setMaxHistorySize,
      testAgentConnection,
      updateAgentSettings,
    ]
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
