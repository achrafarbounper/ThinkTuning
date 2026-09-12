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
import {
  SESSION_KEY,
  isSessionValid,
  readStoredBaseUrl,
  readStoredSession,
  type AuthSession,
} from "../api/authSession";
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
  hfUrl: "https://router.huggingface.co/v1",
  hfApiKey: "",
  lmStudioUrl: "http://192.168.1.184:1234/v1",
  timeoutSeconds: 600,
  contextLength: 2048,
  temperature: 0.2,
  sseFirstEventTimeout: 25,
  sseHeartbeat: 10,
  trainMaxPerLang: 500,
  trainAugmentFraction: 0.4,
  trainVariantsPerExample: 2,
  trainUseBackTranslation: false,
  trainEpochs: 4,
  trainBatchSize: 8,
  trainNumWorkers: 0,
  trainMaxLength: 160,
  trainLearningRate: 3e-5,
  trainWeightDecay: 0.01,
  trainWarmupRatio: 0.1,
  trainDevice: "auto",
  // SCRUM-138 : budgets, log, MCP et flags (module de configuration IHM).
  maxLlmRounds: 6,
  maxToolCalls: 20,
  logLevel: "INFO",
  mcpFirst: false,
  mcpAuthRequired: true,
  // SCRUM-139 : sécurité réseau (bac à sable SSRF) — fail-closed par défaut.
  ssrfEnabled: true,
  ssrfAllowlist: "",
  flagReliability: true,
  flagAudit: true,
  flagToolAnalytics: true,
  flagContext: true,
  flagCopilot: true,
  flagWebsocket: true,
  flagMultiAgent: true,
  flagCustomTools: true,
  flagNewCore: true,
  flagLlmV2: true,
};

/** Lecture + validation de la config API stockée (fusion avec les défauts).
 *
 * P1 SEC (point 10b) : la clé API n'est JAMAIS lue depuis localStorage —
 * le proxy nginx (compose) injecte X-API-Key côté serveur ; en dev sans
 * proxy, l'utilisateur peut saisir une clé (mémoire seule, non persistée).
 * Les clés historiques déjà stockées sont PURGÉES au passage.
 */
function readStoredConfig(): { baseUrl: string } {
  const baseUrl = readStoredBaseUrl(DEFAULT_BASE_URL);
  // Purge défensive (P1 SEC) : une éventuelle clé API persistée par une
  // version antérieure est retirée du stockage — aucun secret en localStorage.
  try {
    const parsed = JSON.parse(
      window.localStorage.getItem("thinktuning.apiConfig") ?? ""
    ) as Partial<ApiConnectionConfig>;
    if (parsed.apiKey) {
      try {
        window.localStorage.setItem(
          "thinktuning.apiConfig",
          JSON.stringify({ baseUrl })
        );
      } catch {
        /* stockage indisponible : rien d'autre à faire */
      }
    }
  } catch {
    /* cas déjà couvert par readStoredBaseUrl */
  }
  return { baseUrl };
}

/** Lecture des paramètres agent stockés (fusion avec les défauts). */
function readStoredAgentSettings(): AgentSettings {
  try {
    const parsed = JSON.parse(
      window.localStorage.getItem("thinktuning.agentSettings") ?? ""
    ) as Partial<AgentSettings>;
    return { ...AGENT_DEFAULTS, ...parsed };
  } catch {
    return { ...AGENT_DEFAULTS };
  }
}

/** Lecture de la taille max d'historique stockée (bornée). */
function readStoredMaxHistorySize(): number {
  try {
    const size = parseInt(
      window.localStorage.getItem("thinktuning.maxHistorySize") ?? "",
      10
    );
    return Number.isNaN(size) ? DEFAULT_MAX_HISTORY : Math.max(1, size);
  } catch {
    return DEFAULT_MAX_HISTORY;
  }
}

export default function AppProvider({ children }: { children: ReactNode }) {
  // Persistance centralisée via useLocalStorage : un seul chemin de lecture/
  // écriture/erreur (fini les try/catch + useEffect dupliqués par champ).
  //
  // P1 SEC (point 10b) : la clé API n'est PLUS persistée — seul baseUrl vit
  // dans localStorage. La clé (repli dev sans proxy) reste EN MÉMOIRE via un
  // useState simple : un rafraîchissement de page la vide volontairement.
  const [configBase, setConfigBase] = useLocalStorage<{ baseUrl: string }>(
    "thinktuning.apiConfig",
    readStoredConfig()
  );
  const [configApiKey, setConfigApiKey] = useState("");
  const config = useMemo<ApiConnectionConfig>(
    () => ({ baseUrl: configBase.baseUrl, apiKey: configApiKey }),
    [configBase.baseUrl, configApiKey]
  );
  const [health, setHealth] = useState<ApiHealth | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [models, setModels] = useState<ModelVersion[]>([]);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [activeModel, setActiveModel] = useState("");
  const [predictionsHistory, setPredictionsHistory] = useLocalStorage<PredictionResult[]>(
    "thinktuning.predictionsHistory",
    []
  );
  const [maxHistorySizeState, setMaxHistorySizeState] = useLocalStorage<number>(
    "thinktuning.maxHistorySize",
    readStoredMaxHistorySize()
  );
  const [agentSettings, setAgentSettings] = useLocalStorage<AgentSettings>(
    "thinktuning.agentSettings",
    readStoredAgentSettings()
  );
  const [agentLoading, setAgentLoading] = useState(false);
  const [agentError, setAgentError] = useState<string | null>(null);
  const [logs, setLogs] = useState<ActivityLog[]>([]);
  const logIdRef = useRef(0);

  // Session d'authentification : le jeton JWT (si valide) est attaché au
  // client en PRIORITÉ sur X-API-Key — le backend accepte les deux modes.
  const [session] = useLocalStorage<AuthSession | null>(
    SESSION_KEY,
    readStoredSession()
  );
  const sessionToken = session && isSessionValid(session) ? session.token : "";
  const client = useMemo(
    () => new SentimentApiClient({ ...config, bearerToken: sessionToken }),
    [config, sessionToken]
  );

  // setConfig exposé tel quel au contexte (SettingsPage) : le baseUrl est
  // persisté, la clé reste mémoire seule (jamais écrite dans localStorage).
  const setConfig = useCallback(
    (next: ApiConnectionConfig) => {
      setConfigBase({ baseUrl: next.baseUrl || DEFAULT_BASE_URL });
      setConfigApiKey(next.apiKey || "");
    },
    [setConfigBase]
  );

  const pushLog = useCallback((type: ActivityLog["type"], text: string) => {
    logIdRef.current += 1;
    setLogs((prev) =>
      [{ id: logIdRef.current, type, text, ts: Date.now() }, ...prev].slice(0, 25)
    );
  }, []);

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
    // Le Bearer JWT suffit (lecture) : le polling tourne dès qu'une session
    // valide existe, qu'une clé API soit mémorisée ou non (P1 SEC).
    if (!config.apiKey && !sessionToken) return;
    try {
      const list = await client.listModels();
      setModels(list ?? []);
      setModelsError(null);
    } catch (err) {
      setModelsError(err instanceof Error ? err.message : String(err));
    }
  }, [client, config.apiKey, sessionToken]);

  // Le polling des modèles est mis en pause sans clé API ni session JWT (flot
  // d'appels inutile) et différé pour rester hors du chemin critique.
  usePolling({
    intervalMs: MODELS_POLL_MS,
    immediate: true,
    initialDelayMs: MODELS_FIRST_DELAY_MS,
    enabled: Boolean(config.apiKey || sessionToken),
    tick: refreshModels,
  });

  const persistAgentSettings = useCallback(
    (settings: AgentSettings | ((prev: AgentSettings) => AgentSettings)) => {
      // useLocalStorage accepte un updater : écriture état + stockage atomique.
      setAgentSettings(settings);
    },
    [setAgentSettings]
  );

  // Réutilise le client memoïsé (même config) au lieu de le réinstancier.
  const updateAgentSettings = useCallback(
    async (updates: Partial<AgentSettings>) => {
      setAgentLoading(true);
      try {
        const resp = await client._request<{ settings?: Record<string, unknown> }>(
          "/api/v1/agent/settings",
          { method: "PUT", body: agentSettingsPayload(updates) }
        );
        if (resp) {
          const normalized = normalizeAgentSettings(resp.settings);
          persistAgentSettings((prev) => ({
            ...normalized,
            openrouterApiKey:
              updates.openrouterApiKey !== undefined
                ? updates.openrouterApiKey
                : prev.openrouterApiKey,
          }));
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
        return await client._request("/api/v1/agent/settings/test", {
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
          "/api/v1/agent/settings",
          { method: "GET" }
        );
        if (res) {
          const normalized = normalizeAgentSettings(res.settings);
          // L'API ne renvoie jamais les clés secrètes : on préserve la valeur
          // locale via un updater (aucune closure obsolète, pas de relecture
          // du stockage).
          persistAgentSettings((prev) => ({
            ...normalized,
            openrouterApiKey: prev.openrouterApiKey,
          }));
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        setAgentError(message);
        pushLog("error", "Impossible de charger les paramètres IA: " + message);
      } finally {
        setAgentLoading(false);
      }
    };
    // Une session JWT valide suffit (lecture) ; sinon il faut une clé API
    // mémorisée en mémoire (Settings) pour charger les réglages de l'agent.
    if (config.apiKey || sessionToken) void loadAgent();
  }, [client, config.apiKey, sessionToken, persistAgentSettings, pushLog]);

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
      // useLocalStorage persiste automatiquement (plus d'écriture manuelle).
      setMaxHistorySizeState(newSize);
      setPredictionsHistory((prev) => prev.slice(0, newSize));
    },
    [setMaxHistorySizeState, setPredictionsHistory]
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
