/**
 * Page « Paramètres » — connexion à l'API FastAPI et préférences locales.
 *
 * La configuration (URL de base + clé X-API-Key) est persistée en localStorage
 * via AppContext ; elle est partagée par toutes les pages et le chat.
 *
 * La section « Assistant IA » configure le provider LLM (Ollama / OpenRouter /
 * Hugging Face Inference Providers / LM Studio).
 */

import { useCallback, useState, type FormEvent } from "react";
import { useApp } from "../context/useApp";
import { DEFAULT_BASE_URL } from "../api/sentimentApiClient";

interface DraftShape {
  baseUrl: string;
  apiKey: string;
  provider: string;
  model: string;
  ollamaUrl: string;
  openrouterUrl: string;
  openrouterApiKey: string;
  hfUrl: string;
  hfApiKey: string;
  lmStudioUrl: string;
  timeoutSeconds: number | string;
  contextLength: number | string;
  temperature: number | string;
  // SCRUM-138 : budgets, log, MCP et flags — module de configuration IHM
  // (stockés/chargés depuis la base MongoDB).
  maxLlmRounds: number | string;
  maxToolCalls: number | string;
  logLevel: string;
  mcpFirst: boolean;
  mcpAuthRequired: boolean;
  // Sécurité réseau (bac à sable SSRF — module de configuration IHM).
  ssrfEnabled: boolean;
  ssrfAllowlist: string;
  flagReliability: boolean;
  flagAudit: boolean;
  flagToolAnalytics: boolean;
  flagContext: boolean;
  flagCopilot: boolean;
  flagWebsocket: boolean;
  flagMultiAgent: boolean;
  flagCustomTools: boolean;
  flagNewCore: boolean;
  flagLlmV2: boolean;
}

/** Libellés UI des feature flags (module de configuration IHM — base MongoDB). */
const AGENT_FLAG_LABELS: Array<{ key: keyof DraftShape; label: string; hint?: string }> = [
  { key: "flagReliability", label: "Fiabilité & retry", hint: "Retry + circuit breaker des appels LLM." },
  { key: "flagAudit", label: "Audit", hint: "Journalisation des actions de l'agent." },
  { key: "flagToolAnalytics", label: "Statistiques d'outils", hint: "Analytics d'utilisation des outils." },
  { key: "flagContext", label: "Contexte", hint: "Gestion mémoire / contexte de session." },
  { key: "flagCopilot", label: "Copilot", hint: "Suggestions de réponses." },
  { key: "flagWebsocket", label: "WebSocket", hint: "Streaming temps réel du chat." },
  { key: "flagMultiAgent", label: "Multi-agents", hint: "Orchestration Lead/Worker." },
  { key: "flagCustomTools", label: "Tools personnalisés", hint: "Registre des outils dynamiques (SCRUM-99)." },
  { key: "flagNewCore", label: "Noyau agentique v2", hint: "Bascule AGENT_NEW_CORE." },
  { key: "flagLlmV2", label: "Client LLM v2", hint: "Bascule AGENT_LLM_V2 (HttpLLMClient)." },
];

export default function SettingsPage() {
    const {
    config,
    setConfig,
    agentSettings,
    updateAgentSettings,
    testAgentConnection,
    agentLoading,
    agentError,
    pushLog,
  } = useApp();

  // Draft local : ne modifie pas l'état global tant qu'on n'a pas sauvegardé.
  const [draft, setDraft] = useState<DraftShape>(() => ({
    baseUrl: config.baseUrl || DEFAULT_BASE_URL,
    apiKey: config.apiKey || "",
    provider: agentSettings?.provider ?? "ollama",
    model: agentSettings?.model ?? "",
    ollamaUrl: agentSettings?.ollamaUrl ?? "",
    openrouterUrl: agentSettings?.openrouterUrl ?? "https://openrouter.ai/api/v1",
    openrouterApiKey: agentSettings?.openrouterApiKey ?? "",
    hfUrl: agentSettings?.hfUrl ?? "https://router.huggingface.co/v1",
    hfApiKey: agentSettings?.hfApiKey ?? "",
    lmStudioUrl: agentSettings?.lmStudioUrl ?? "http://192.168.1.184:1234/v1",
    timeoutSeconds: agentSettings?.timeoutSeconds ?? 60,
    contextLength: agentSettings?.contextLength ?? 512,
    temperature: agentSettings?.temperature ?? 0.2,
    maxLlmRounds: agentSettings?.maxLlmRounds ?? 6,
    maxToolCalls: agentSettings?.maxToolCalls ?? 20,
    logLevel: agentSettings?.logLevel ?? "INFO",
    mcpFirst: agentSettings?.mcpFirst ?? false,
    mcpAuthRequired: agentSettings?.mcpAuthRequired ?? true,
    ssrfEnabled: agentSettings?.ssrfEnabled ?? true,
    ssrfAllowlist: agentSettings?.ssrfAllowlist ?? "",
    flagReliability: agentSettings?.flagReliability ?? true,
    flagAudit: agentSettings?.flagAudit ?? true,
    flagToolAnalytics: agentSettings?.flagToolAnalytics ?? true,
    flagContext: agentSettings?.flagContext ?? true,
    flagCopilot: agentSettings?.flagCopilot ?? true,
    flagWebsocket: agentSettings?.flagWebsocket ?? true,
    flagMultiAgent: agentSettings?.flagMultiAgent ?? true,
    flagCustomTools: agentSettings?.flagCustomTools ?? true,
    flagNewCore: agentSettings?.flagNewCore ?? true,
    flagLlmV2: agentSettings?.flagLlmV2 ?? true,
  }));

  const updateDraft = useCallback(
    <K extends keyof DraftShape>(field: K, value: DraftShape[K]) => {
      setDraft((prev) => ({ ...prev, [field]: value }));
    },
    []
  );

  // Reflète les paramètres agent une fois chargés via l'API (nouvel onglet /
  // navigateur après enregistrement) sans écraser la connexion API. Mise à jour
  // en phase de rendu (pattern React documenté) pour éviter un `useEffect`.
  const [prevAgent, setPrevAgent] = useState(agentSettings);
  if (agentSettings !== prevAgent) {
    setPrevAgent(agentSettings);
    setDraft((prev) => ({ ...prev, ...(agentSettings || {}) }));
  }

  const isOllama = draft.provider === "ollama";
  const isHf = draft.provider === "hf";
  const isLmStudio = draft.provider === "lm_studio";

  // -- Connexion API ----------------------------------------------------------
    const saveConfig = useCallback(
    (e: FormEvent) => {
      e.preventDefault();
      setConfig({ baseUrl: draft.baseUrl, apiKey: draft.apiKey });
      pushLog("info", `Configuration mise à jour → ${draft.baseUrl}`);
    },
    [draft, setConfig, pushLog]
  );

  // -- Assistant IA -----------------------------------------------------------
  const handleTestAgentConnection = useCallback(async () => {
    try {
      await testAgentConnection(draft);
      pushLog("success", "Test de connexion IA réussi !");
    } catch (err) {
      pushLog("error", `Test échoué : ${err instanceof Error ? err.message : "?"}`);
    }
  }, [draft, testAgentConnection, pushLog]);

  const handleSaveAgentSettings = useCallback(async () => {
    try {
      await updateAgentSettings(draft);
      pushLog("success", "Paramètres de l'assistant IA enregistrés.");
    } catch (err) {
      pushLog("error", `Échec de l'enregistrement : ${err instanceof Error ? err.message : "?"}`);
    }
  }, [draft, updateAgentSettings, pushLog]);

  // -- Préférences ------------------------------------------------------------
  const savePreferences = useCallback(
    (e: FormEvent) => {
      e.preventDefault();
          pushLog("info", "Préférences appliquées.");
    },
    [pushLog]
  );

  return (
    <>
      <header className="page-head">
        <h1>Paramètres</h1>
        <p>Connexion à l'API ThinkTuning et préférences du dashboard.</p>
      </header>

      <div className="page-body">
        {/* Connexion API */}
        <section className="tt-panel">
          <div className="tt-panel-head">
            <h2>Connexion API</h2>
            <span className={`tt-tag ${config.apiKey ? "tt-tag-status-completed" : "tt-tag-status-pending"}`}>
              {config.apiKey ? "configurée" : "manquante"}
            </span>
          </div>
          <form onSubmit={saveConfig} className="tt-form tt-settings-form-page">
            <label>
              <span className="tt-assistant-label">URL de base</span>
              <input
                type="text"
                value={draft.baseUrl}
                onChange={(e) => updateDraft("baseUrl", e.target.value)}
                placeholder="http://localhost:8000"
              />
            </label>
            <label>
              <span className="tt-assistant-label">Clé API (X-API-Key)</span>
              <input
                type="password"
                value={draft.apiKey || ""}
                onChange={(e) => updateDraft("apiKey", e.target.value)}
                placeholder="API_KEY côté serveur"
              />
            </label>
            <button type="submit" className="tt-btn tt-btn-primary">
              Enregistrer
            </button>
          </form>
                </section>

        {/* Assistant IA */}
        <section className="tt-panel tt-settings-assistant-panel">
          <div className="tt-panel-head">
            <h2>Assistant IA</h2>
            <span className={`tt-tag ${agentLoading ? "tt-tag-status-pending" : "tt-tag-status-completed"}`}>
              {agentLoading ? "sauvegarde..." : "prêt"}
            </span>
          </div>

          {/* Provider selector */}
          <div className="tt-assistant-section">
            <span className="tt-assistant-label">Provider LLM</span>
            <select
              value={draft.provider}
              onChange={(e) => {
                updateDraft("provider", e.target.value);
                if (e.target.value === "openrouter") {
                  updateDraft("openrouterUrl", "https://openrouter.ai/api/v1");
                } else if (e.target.value === "hf") {
                  updateDraft("hfUrl", "https://router.huggingface.co/v1");
                } else if (e.target.value === "lm_studio") {
                  updateDraft("lmStudioUrl", "http://192.168.1.184:1234/v1");
                } else {
                  updateDraft("ollamaUrl", "");
                }
              }}
              className="tt-select-tt-settings"
            >
              <option value="ollama">Ollama (local)</option>
              <option value="openrouter">OpenRouter (hébergé)</option>
              <option value="hf">Hugging Face (Inference Providers)</option>
              <option value="lm_studio">LM Studio (local)</option>
            </select>
          </div>

          <p className="tt-assistant-section-help">
            {isOllama
              ? "Exécute les requêtes LLM locales via Docker Ollama."
              : isHf
                ? "Accède aux LLM via Hugging Face Inference Providers avec votre token HF."
                : isLmStudio
                  ? "Serveur local LM Studio — démarrez le serveur (onglet Developer) ; aucune clé requise."
                  : "Accède aux LLM via OpenRouter avec votre clé API."}
          </p>

          <div className="tt-assistant-grid">
            <label>
              <span className="tt-assistant-label">Modèle (ID)</span>
              <input
                type="text"
                value={draft.model}
                onChange={(e) => updateDraft("model", e.target.value)}
                placeholder={
                  isOllama
                    ? "qwen2.5:0.5b"
                    : isLmStudio
                      ? "modèle chargé dans LM Studio (vide = modèle actif)"
                      : "vendor/openai/gpt-3.5-turbo"
                }
                className="tt-input-tt-settings"
              />
            </label>

            <label>
              <span className="tt-assistant-label">Timeout (s)</span>
              <input
                type="number"
                min="10"
                max="600"
                value={draft.timeoutSeconds || 60}
                onChange={(e) => updateDraft("timeoutSeconds", Number(e.target.value))}
                                className="tt-input-tt-settings"
              />
            </label>

            <label>
              <span className="tt-assistant-label">Context length (tokens)</span>
              <input
                type="number"
                min="128"
                max="32768"
                value={draft.contextLength || 512}
                onChange={(e) => updateDraft("contextLength", Number(e.target.value))}
                className="tt-input-tt-settings"
              />
            </label>

            <label>
              <span className="tt-assistant-label">Temperature (0.0-2.0)</span>
              <input
                type="number"
                step="0.1"
                min="0"
                max="2"
                value={draft.temperature ?? 0.2}
                onChange={(e) => updateDraft("temperature", Number(e.target.value))}
                className="tt-input-tt-settings"
              />
            </label>
          </div>

          {/* Champs conditionnels selon le provider */}
          {isOllama && (
            <label>
              <span className="tt-assistant-label">URL Ollama</span>
              <input
                type="text"
                value={draft.ollamaUrl}
                onChange={(e) => updateDraft("ollamaUrl", e.target.value)}
                placeholder="http://localhost:11434"
                className="tt-input-tt-settings"
              />
            </label>
          )}

          {!isOllama && !isHf && !isLmStudio && (
            <label>
              <span className="tt-assistant-label">URL OpenRouter</span>
              <input
                type="text"
                value={draft.openrouterUrl}
                onChange={(e) => updateDraft("openrouterUrl", e.target.value)}
                placeholder="https://openrouter.ai/api/v1"
                className="tt-input-tt-settings"
              />
            </label>
          )}

          {!isOllama && !isHf && !isLmStudio && (
            <label>
              <span className="tt-assistant-label">Clé API OpenRouter</span>
              <input
                type="password"
                value={draft.openrouterApiKey}
                onChange={(e) => updateDraft("openrouterApiKey", e.target.value)}
                placeholder="sk-or-xxxxxxxxxxxx"
                className="tt-input-tt-settings"
              />
            </label>
          )}

          {isLmStudio && (
            <label>
              <span className="tt-assistant-label">URL LM Studio</span>
              <input
                type="text"
                value={draft.lmStudioUrl}
                onChange={(e) => updateDraft("lmStudioUrl", e.target.value)}
                placeholder="http://192.168.1.184:1234/v1"
                className="tt-input-tt-settings"
              />
            </label>
          )}

          {isHf && (
            <label>
              <span className="tt-assistant-label">URL Hugging Face</span>
              <input
                type="text"
                value={draft.hfUrl}
                onChange={(e) => updateDraft("hfUrl", e.target.value)}
                placeholder="https://router.huggingface.co/v1"
                className="tt-input-tt-settings"
              />
            </label>
          )}

          {isHf && (
            <label>
              <span className="tt-assistant-label">Token API Hugging Face</span>
              <input
                type="password"
                value={draft.hfApiKey}
                onChange={(e) => updateDraft("hfApiKey", e.target.value)}
                placeholder="hf_xxxxxxxxxxxxxxxx"
                className="tt-input-tt-settings"
              />
            </label>
          )}

          {/* Budgets & garde-fous (module IHM — base MongoDB) */}
          <div className="tt-assistant-section">
            <span className="tt-assistant-label">Budgets & garde-fous</span>
            <div className="tt-assistant-grid">
              <label>
                <span className="tt-assistant-label">Rounds LLM max par run</span>
                <input
                  type="number"
                  min="1"
                  max="50"
                  value={draft.maxLlmRounds || 6}
                  onChange={(e) => updateDraft("maxLlmRounds", Number(e.target.value))}
                  className="tt-input-tt-settings"
                />
              </label>
              <label>
                <span className="tt-assistant-label">Appels d'outils max par run</span>
                <input
                  type="number"
                  min="1"
                  max="200"
                  value={draft.maxToolCalls || 20}
                  onChange={(e) => updateDraft("maxToolCalls", Number(e.target.value))}
                  className="tt-input-tt-settings"
                />
              </label>
            </div>
          </div>

          {/* Observabilité */}
          <div className="tt-assistant-section">
            <span className="tt-assistant-label">Niveau de log</span>
            <select
              value={draft.logLevel}
              onChange={(e) => updateDraft("logLevel", e.target.value)}
              className="tt-select-tt-settings"
            >
              <option value="DEBUG">DEBUG</option>
              <option value="INFO">INFO</option>
              <option value="WARNING">WARNING</option>
              <option value="ERROR">ERROR</option>
            </select>
            <p className="tt-assistant-section-help">
              Niveau des journaux du noyau agent (persisté dans la base).
            </p>
          </div>

          {/* Surface MCP */}
          <div className="tt-assistant-section">
            <span className="tt-assistant-label">Surface MCP</span>
            <label className="tt-assistant-checkbox">
              <input
                type="checkbox"
                checked={draft.mcpFirst}
                onChange={(e) => updateDraft("mcpFirst", e.target.checked)}
              />
              MCP-First — surface HTTP legacy de l'agent en lecture seule
            </label>
            <label className="tt-assistant-checkbox">
              <input
                type="checkbox"
                checked={draft.mcpAuthRequired}
                onChange={(e) => updateDraft("mcpAuthRequired", e.target.checked)}
              />
              Auth X-API-Key obligatoire sur le transport MCP
            </label>
          </div>

          {/* Sécurité réseau (bac à sable SSRF) */}
          <div className="tt-assistant-section">
            <span className="tt-assistant-label">Sécurité réseau (SSRF)</span>
            <label className="tt-assistant-checkbox">
              <input
                type="checkbox"
                checked={draft.ssrfEnabled}
                onChange={(e) => updateDraft("ssrfEnabled", e.target.checked)}
              />
              Protection SSRF — interdire les hôtes privés / loopback
            </label>
            <label>
              <span className="tt-assistant-label">Hôtes privés autorisés (CSV)</span>
              <input
                type="text"
                value={draft.ssrfAllowlist}
                disabled={!draft.ssrfEnabled}
                onChange={(e) => updateDraft("ssrfAllowlist", e.target.value)}
                placeholder="127.0.0.1,localhost,searxng"
                className="tt-input-tt-settings"
              />
            </label>
            <p className="tt-assistant-section-help">
              Ex. « 127.0.0.1,localhost,searxng » pour joindre une instance SearXNG
              locale via web_search. Appliqué dès l'enregistrement, sans
              redémarrage (surclasse les variables d'environnement).
            </p>
          </div>

          {/* Feature flags (AGENT_<NOM> historique) */}
          <div className="tt-assistant-section">
            <span className="tt-assistant-label">Fonctionnalités</span>
            <p className="tt-assistant-section-help">
              Bascules persistées via le module de configuration IHM (base
              MongoDB) — appliquées lors du prochain run de l'agent.
            </p>
            {AGENT_FLAG_LABELS.map(({ key, label, hint }) => (
              <label key={key} className="tt-assistant-checkbox">
                <input
                  type="checkbox"
                  checked={Boolean(draft[key])}
                  onChange={(e) => updateDraft(key, e.target.checked)}
                />
                <span>{label}</span>
                {hint && <small className="tt-assistant-section-help">{hint}</small>}
              </label>
            ))}
          </div>

          <div className="tt-assistant-actions" style={{ marginTop: "1.25rem", display: "flex", gap: "0.75rem", flexWrap: "wrap" }}>
            <button
              type="button"
              className="tt-btn tt-btn-ghost"
              onClick={handleTestAgentConnection}
              disabled={agentLoading}
            >
              {agentLoading ? "Test..." : "Tester la connexion"}
            </button>
            <button
              type="button"
              className="tt-btn tt-btn-primary"
              onClick={handleSaveAgentSettings}
              disabled={agentLoading}
            >
              {agentLoading ? "Enregistrement..." : "Enregistrer"}
            </button>
          </div>

          {agentError && <div className="tt-alert tt-alert-error">{agentError}</div>}
        </section>

        {/* Préférences */}
        <section className="tt-panel">
          <div className="tt-panel-head">
            <h2>Préférences</h2>
          </div>
          <form className="tt-form tt-settings-form-page" onSubmit={savePreferences}>
            <label>
              <span className="tt-assistant-label">Prédictions conservées (max)</span>
              <input
                type="number"
                min="1"
                max="1000"
                value={30}
                readOnly
                className="tt-input-tt-settings"
              />
            </label>
            <button type="submit" className="tt-btn tt-btn-ghost">
              Appliquer
            </button>
          </form>
          <p className="tt-hint">
            Historique stocké localement (localStorage).
          </p>
        </section>

        {/* À propos */}
        <section className="tt-panel">
          <div className="tt-panel-head">
            <h2>À propos</h2>
          </div>
          <p className="tt-hint">
            ThinkTuning — pipeline complet de recomposition de données (EDA) +
            fine-tuning DistilBERT multilingue pour la classification de sentiments
            (positif / neutre / négatif) en français et en anglais. Assistant IA
            configurable via Ollama, OpenRouter, Hugging Face ou LM Studio.
          </p>
        </section>
      </div>
    </>
  );
}


