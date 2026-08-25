/**
 * Provider global du dashboard : configuration API, santé, modèles,
 * historique des prédictions et journal d'activité.
 *
 * Le hook d'accès est dans useApp.js ; le contexte brut dans appContext.js.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SentimentApiClient } from "../api/sentimentApiClient";
import { AppContext } from "./appContext";

const CONFIG_STORAGE_KEY = "thinktuning.apiConfig";
const PREDICTIONS_HISTORY_KEY = "thinktuning.predictionsHistory";
const MAX_HISTORY_SIZE_KEY = "thinktuning.maxHistorySize";
const HEALTH_POLL_MS = 8000;
const MODELS_POLL_MS = 15000;
const DEFAULT_MAX_HISTORY = 20;

function loadStoredConfig() {
  try {
    const raw = window.localStorage.getItem(CONFIG_STORAGE_KEY);
    if (!raw) return { baseUrl: "http://localhost:8000", apiKey: "" };
    const parsed = JSON.parse(raw);
    return {
      baseUrl: parsed.baseUrl || "http://localhost:8000",
      apiKey: parsed.apiKey || "",
    };
  } catch {
    return { baseUrl: "http://localhost:8000", apiKey: "" };
  }
}

function loadStoredPredictionsHistory() {
  try {
    const raw = window.localStorage.getItem(PREDICTIONS_HISTORY_KEY);
    if (!raw) return [];
    return JSON.parse(raw);
  } catch {
    return [];
  }
}

function loadStoredMaxHistorySize() {
  try {
    const raw = window.localStorage.getItem(MAX_HISTORY_SIZE_KEY);
    if (!raw) return DEFAULT_MAX_HISTORY;
    const size = parseInt(raw, 10);
    return Number.isNaN(size) ? DEFAULT_MAX_HISTORY : Math.max(1, size);
  } catch {
    return DEFAULT_MAX_HISTORY;
  }
}

function saveHistoryToStorage(history) {
  try {
    window.localStorage.setItem(PREDICTIONS_HISTORY_KEY, JSON.stringify(history));
  } catch {
    /* stockage indisponible */
  }
}

export default function AppProvider({ children }) {
  const [config, setConfig] = useState(loadStoredConfig);

  // Client reconstruit uniquement quand la configuration change ;
  // aucune lecture de ref pendant le rendu (règle react-hooks/refs).
  const client = useMemo(() => new SentimentApiClient(config), [config]);

  const [health, setHealth] = useState(null);
  const [healthError, setHealthError] = useState(null);

  const [models, setModels] = useState([]);
  const [modelsError, setModelsError] = useState(null);
  const [activeModel, setActiveModel] = useState("");

  const [predictionsHistory, setPredictionsHistory] = useState(
    loadStoredPredictionsHistory
  );
  const [maxHistorySize, setMaxHistorySizeState] = useState(
    loadStoredMaxHistorySize
  );

  const [logs, setLogs] = useState([]);
  const logIdRef = useRef(0);

  const pushLog = useCallback((type, text) => {
    logIdRef.current += 1;
    setLogs((prev) =>
      [{ id: logIdRef.current, type, text, ts: Date.now() }, ...prev].slice(0, 25)
    );
  }, []);

  // -- Config : persistance locale -------------------------------------------
  useEffect(() => {
    try {
      window.localStorage.setItem(CONFIG_STORAGE_KEY, JSON.stringify(config));
    } catch {
      /* stockage indisponible */
    }
  }, [config]);

  // -- Santé : GET /health, sans clé API -------------------------------------
  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const result = await client.getHealth();
        if (!cancelled) {
          setHealth(result);
          setHealthError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setHealth(null);
          setHealthError(err.message);
        }
      }
    };
    poll();
    const interval = setInterval(poll, HEALTH_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [client]);

  // -- Modèles : nécessite la clé API, rafraîchissement périodique ----------
  const refreshModels = useCallback(async () => {
    if (!config.apiKey) return;
    try {
      const list = await client.listModels();
      setModels(list);
      setModelsError(null);
    } catch (err) {
      setModelsError(err.message);
    }
  }, [client, config.apiKey]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      if (!config.apiKey) return;
      try {
        const list = await client.listModels();
        if (!cancelled) {
          setModels(list);
          setModelsError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setModelsError(err.message);
        }
      }
    };
    load();
    const interval = setInterval(load, MODELS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [client, config.apiKey]);

  // -- Historique des prédictions --------------------------------------------
  const addToHistory = useCallback(
    (newPredictions) => {
      setPredictionsHistory((prev) => {
        const updated = [
          ...newPredictions.map((pred) => ({ ...pred, timestamp: Date.now() })),
          ...prev,
        ].slice(0, maxHistorySize);
        saveHistoryToStorage(updated);
        return updated;
      });
    },
    [maxHistorySize]
  );

  const clearHistory = useCallback(() => {
    setPredictionsHistory([]);
    saveHistoryToStorage([]);
    pushLog("info", "Historique des prédictions effacé.");
  }, [pushLog]);

  const setMaxHistorySize = useCallback((size) => {
    const parsed = Number(size);
    const newSize =
      size === "" || Number.isNaN(parsed)
        ? DEFAULT_MAX_HISTORY
        : Math.max(1, Math.min(1000, parsed));
    setMaxHistorySizeState(newSize);
    try {
      window.localStorage.setItem(MAX_HISTORY_SIZE_KEY, String(newSize));
    } catch {
      /* stockage indisponible */
    }
    setPredictionsHistory((prev) => {
      const trimmed = prev.slice(0, newSize);
      saveHistoryToStorage(trimmed);
      return trimmed;
    });
  }, []);

  const value = useMemo(
    () => ({
      config,
      setConfig,
      client,
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
      maxHistorySize,
      setMaxHistorySize,
      logs,
      pushLog,
    }),
    [
      config,
      client,
      health,
      healthError,
      models,
      modelsError,
      refreshModels,
      activeModel,
      predictionsHistory,
      addToHistory,
      clearHistory,
      maxHistorySize,
      setMaxHistorySize,
      logs,
      pushLog,
    ]
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
