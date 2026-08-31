/**
 * Provider global du dashboard : configuration API, santé, modèles,
 * historique des prédictions, journal d'activité et paramètres de l'assistant IA.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SentimentApiClient, agentSettingsPayload, normalizeAgentSettings, DEFAULT_BASE_URL } from "../api/sentimentApiClient";
import { usePolling } from "../hooks/usePolling";
import { AppContext } from "./appContext";

const DEFAULT_MAX_HISTORY = 20;
const HEALTH_POLL_MS = 8000;
const MODELS_POLL_MS = 15000;
// Différage du premier poll hors du chemin critique : le /health (450–502 ms)
// et le listModels ne bloquent plus le rendu initial ni le LCP.
const HEALTH_FIRST_DELAY_MS = 2000;
const MODELS_FIRST_DELAY_MS = 2500;
const AGENT_DEFAULTS = {
  provider: "ollama",
  model: "",
  ollamaUrl: "",
  openrouterUrl: "https://openrouter.ai/api/v1",
  openrouterApiKey: "",
  timeoutSeconds: 60,
  contextLength: 512,
  temperature: 0.2,
};

function loadStoredConfig() {
  try {
    const raw = window.localStorage.getItem("thinktuning.apiConfig");
    if (!raw) return { baseUrl: DEFAULT_BASE_URL, apiKey: "" };
    const parsed = JSON.parse(raw);
    return { baseUrl: parsed.baseUrl || DEFAULT_BASE_URL, apiKey: parsed.apiKey || "" };
  } catch { return { baseUrl: DEFAULT_BASE_URL, apiKey: "" }; }
}

function loadAgentSettings() {
  try {
    const raw = window.localStorage.getItem("thinktuning.agentSettings");
    if (!raw) return { ...AGENT_DEFAULTS };
    return { ...AGENT_DEFAULTS, ...JSON.parse(raw) };
  } catch { return { ...AGENT_DEFAULTS }; }
}

function saveAgentSettings(settings) {
  try { window.localStorage.setItem("thinktuning.agentSettings", JSON.stringify(settings)); } catch { /* stockage indisponible */ }
}

function loadStoredPredictionsHistory() {
  try {
    const raw = window.localStorage.getItem("thinktuning.predictionsHistory");
    return raw ? JSON.parse(raw) : [];
  } catch { return []; }
}

function loadStoredMaxHistorySize() {
  try {
    const raw = window.localStorage.getItem("thinktuning.maxHistorySize");
    const size = parseInt(raw, 10);
    return Number.isNaN(size) ? DEFAULT_MAX_HISTORY : Math.max(1, size);
  } catch { return DEFAULT_MAX_HISTORY; }
}

export default function AppProvider({ children }) {
  const [config, setConfig] = useState(loadStoredConfig);
  const [health, setHealth] = useState(null);
  const [healthError, setHealthError] = useState(null);
  const [models, setModels] = useState([]);
  const [modelsError, setModelsError] = useState(null);
  const [activeModel, setActiveModel] = useState("");
  const [predictionsHistory, setPredictionsHistory] = useState(loadStoredPredictionsHistory);
  const [maxHistorySize, setMaxHistorySizeState] = useState(loadStoredMaxHistorySize);
  const [agentSettings, setAgentSettings] = useState(loadAgentSettings);
  const [agentLoading, setAgentLoading] = useState(false);
  const [agentError, setAgentError] = useState(null);
  const [logs, setLogs] = useState([]);
  const logIdRef = useRef(0);
  const client = useMemo(() => new SentimentApiClient(config), [config]);

  const pushLog = useCallback((type, text) => {
    logIdRef.current += 1;
    setLogs((prev) => [{ id: logIdRef.current, type, text, ts: Date.now() }, ...prev].slice(0, 25));
  }, []);

  useEffect(() => {
    try { window.localStorage.setItem("thinktuning.apiConfig", JSON.stringify(config)); } catch { /* stockage indisponible */ }
  }, [config]);

  // --- Polling santé & modèles (pause auto quand l'onglet est masqué) --------
  // Le premier tick est différé (initialDelayMs) : /health et listModels sont
  // sortis du chemin critique. Le rendu initial et le LCP ne bloquent plus sur
  // ces requêtes non essentielles (auparavant immédiates = ~450–502 ms).
  const pollHealth = useCallback(async () => {
    try { const result = await client.getHealth(); setHealth(result); setHealthError(null); }
    catch (err) { setHealth(null); setHealthError(err.message); }
  }, [client]);

  usePolling({ intervalMs: HEALTH_POLL_MS, immediate: true, initialDelayMs: HEALTH_FIRST_DELAY_MS, tick: pollHealth });

  const refreshModels = useCallback(async () => {
    if (!config.apiKey) return;
    try { const list = await client.listModels(); setModels(list); setModelsError(null); }
    catch (err) { setModelsError(err.message); }
  }, [client, config.apiKey]);

  // Le polling des modèles est purement informatif côté API key, on le met en
  // pause quand la clé est absente pour éviter un flot d'appels inutiles.
  // Il est en plus différé (initialDelayMs) pour rester hors du chemin critique.
  usePolling({
    intervalMs: MODELS_POLL_MS,
    immediate: true,
    initialDelayMs: MODELS_FIRST_DELAY_MS,
    enabled: Boolean(config.apiKey),
    tick: refreshModels,
  });

  const persistAgentSettings = useCallback((settings) => {
    saveAgentSettings(settings);
    setAgentSettings(settings);
  }, []);

  const updateAgentSettings = useCallback(async (updates) => {
    setAgentLoading(true);
    try {
      const c = new SentimentApiClient({ baseUrl: config.baseUrl, apiKey: config.apiKey });
      const resp = await c._request("/api/agent/settings", { method: "PUT", body: agentSettingsPayload(updates) });
      if (resp) {
        const normalized = normalizeAgentSettings(resp.settings);
        // La clé n'est jamais renvoyée par l'API : on conserve celle saisie.
        persistAgentSettings({ ...normalized, openrouterApiKey: updates.openrouterApiKey || normalized.openrouterApiKey });
      }
      setAgentError(null);
      pushLog("success", "Paramètres assistant IA enregistrés.");
    } catch (err) {
      setAgentError(err.message);
      pushLog("error", "Échec enregistrement IA: " + err.message);
      throw err;
    } finally { setAgentLoading(false); }
  }, [config, persistAgentSettings, pushLog]);

  const testAgentConnection = useCallback(async (testParams) => {
    setAgentLoading(true);
    try {
      const c = new SentimentApiClient({ baseUrl: config.baseUrl, apiKey: config.apiKey });
      return await c._request("/api/agent/settings/test", { method: "POST", body: agentSettingsPayload(testParams) });
    } finally { setAgentLoading(false); }
  }, [config]);

  useEffect(() => {
    const loadAgent = async () => {
      setAgentLoading(true);
      try {
        const c = new SentimentApiClient({ baseUrl: config.baseUrl, apiKey: config.apiKey });
        const res = await c._request("/api/agent/settings", { method: "GET" });
        if (res) {
          const normalized = normalizeAgentSettings(res.settings);
          // La clé n'est jamais renvoyée par l'API : on réutilise la valeur locale.
          const local = loadAgentSettings();
          persistAgentSettings({ ...normalized, openrouterApiKey: local.openrouterApiKey || normalized.openrouterApiKey });
        }
      } catch (err) { setAgentError(err.message); pushLog("error", "Impossible de charger les paramètres IA: " + err.message); }
      finally { setAgentLoading(false); }
    };
        if (config.apiKey) loadAgent();
  }, [config, persistAgentSettings, pushLog]);

  const addToHistory = useCallback((newPreds) => {
    setPredictionsHistory((prev) => {
      const updated = [...newPreds.map((p) => ({ ...p, timestamp: Date.now() })), ...prev].slice(0, maxHistorySize);
      try { window.localStorage.setItem("thinktuning.predictionsHistory", JSON.stringify(updated)); } catch { /* stockage indisponible */ }
      return updated;
    });
  }, [maxHistorySize]);

  const clearHistory = useCallback(() => {
    setPredictionsHistory([]);
    try { window.localStorage.removeItem("thinktuning.predictionsHistory"); } catch { /* stockage indisponible */ }
    pushLog("info", "Historique des prédictions effacé.");
  }, [pushLog]);

  const setMaxHistorySize = useCallback((size) => {
    const parsed = Number(size);
    const newSize = size === "" || Number.isNaN(parsed) ? DEFAULT_MAX_HISTORY : Math.max(1, Math.min(1000, parsed));
    setMaxHistorySizeState(newSize);
    try { window.localStorage.setItem("thinktuning.maxHistorySize", String(newSize)); } catch { /* stockage indisponible */ }
    setPredictionsHistory((prev) => { const trimmed = prev.slice(0, newSize); try { window.localStorage.setItem("thinktuning.predictionsHistory", JSON.stringify(trimmed)); } catch { /* stockage indisponible */ } return trimmed; });
  }, []);

  const saveConfig = useCallback((c) => {
    setConfig(c);
    pushLog("info", "Configuration mise à jour → " + c.baseUrl);
  }, [pushLog]);

  const value = useMemo(() => ({
    client,
    config, setConfig, saveConfig,
    agentSettings, persistAgentSettings, updateAgentSettings, testAgentConnection,
    agentLoading, agentError, setAgentError,
    health, healthError, models, modelsError, refreshModels,
    activeModel, setActiveModel,
    predictionsHistory, addToHistory, clearHistory,
    maxHistorySize, setMaxHistorySize,
    logs, pushLog,
  }), [client, config, agentSettings, agentLoading, agentError, health, healthError, models, modelsError, activeModel, predictionsHistory, maxHistorySize, logs, pushLog, addToHistory, clearHistory, persistAgentSettings, refreshModels, saveConfig, setMaxHistorySize, testAgentConnection, updateAgentSettings]);

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}



