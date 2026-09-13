import { useCallback, type FormEvent } from "react";
import { DEFAULT_BASE_URL } from "../api/sentimentApiClient";
import { useApp } from "../context/useApp";
import { AboutSection } from "./settings/AboutSection";
import { AgentSettingsSection } from "./settings/AgentSettingsSection";
import { TrainingSettingsSection } from "./settings/TrainingSettingsSection";
import { ApiConnectionSection } from "./settings/ApiConnectionSection";
import { PreferencesSection } from "./settings/PreferencesSection";
import { useSettingsDraft } from "./settings/useSettingsDraft";

export default function SettingsPage() {
  const {
    config, setConfig, updateAgentSettings, testAgentConnection,
    agentLoading, agentError, pushLog, maxHistorySize, setMaxHistorySize,
  } = useApp();
  const { draft, updateDraft } = useSettingsDraft();

  const saveConfig = useCallback((event: FormEvent) => {
    event.preventDefault();
    setConfig({ baseUrl: draft.baseUrl || DEFAULT_BASE_URL, apiKey: draft.apiKey });
    pushLog("info", `Configuration mise à jour → ${draft.baseUrl}`);
  }, [draft.baseUrl, draft.apiKey, pushLog, setConfig]);

  const testConnection = useCallback(async () => {
    try {
      await testAgentConnection(draft);
      pushLog("success", "Test de connexion IA réussi !");
    } catch (error) {
      pushLog("error", `Test échoué : ${error instanceof Error ? error.message : "?"}`);
    }
  }, [draft, pushLog, testAgentConnection]);

  const saveAgent = useCallback(async (overrides: Partial<typeof draft> = {}) => {
    const values = { ...draft, ...overrides };
    try {
      await updateAgentSettings({
        provider: values.provider, model: values.model, ollamaUrl: values.ollamaUrl,
        openrouterUrl: values.openrouterUrl, openrouterApiKey: values.openrouterApiKey,
        hfUrl: values.hfUrl, hfApiKey: values.hfApiKey, lmStudioUrl: values.lmStudioUrl,
        timeoutSeconds: values.timeoutSeconds, contextLength: values.contextLength,
        temperature: values.temperature, sseFirstEventTimeout: values.sseFirstEventTimeout,
        sseHeartbeat: values.sseHeartbeat, maxLlmRounds: values.maxLlmRounds,
        maxToolCalls: values.maxToolCalls, logLevel: values.logLevel, mcpFirst: values.mcpFirst,
        mcpAuthRequired: values.mcpAuthRequired, ssrfEnabled: values.ssrfEnabled,
        ssrfAllowlist: values.ssrfAllowlist, flagReliability: values.flagReliability,
        flagAudit: values.flagAudit, flagToolAnalytics: values.flagToolAnalytics,
        flagContext: values.flagContext, flagCopilot: values.flagCopilot,
        flagWebsocket: values.flagWebsocket, flagMultiAgent: values.flagMultiAgent,
        flagCustomTools: values.flagCustomTools, flagNewCore: values.flagNewCore, flagLlmV2: values.flagLlmV2,
      });
      pushLog("success", "Paramètres de l'assistant IA enregistrés.");
    } catch (error) {
      pushLog("error", `Échec de l'enregistrement : ${error instanceof Error ? error.message : "?"}`);
    }
  }, [draft, pushLog, updateAgentSettings]);

  const saveTraining = useCallback(async () => {
    try {
      await updateAgentSettings({
        trainMaxPerLang: draft.trainMaxPerLang,
        trainAugmentFraction: draft.trainAugmentFraction,
        trainVariantsPerExample: draft.trainVariantsPerExample,
        trainUseBackTranslation: draft.trainUseBackTranslation,
        trainEpochs: draft.trainEpochs,
        trainBatchSize: draft.trainBatchSize,
        trainNumWorkers: draft.trainNumWorkers,
        trainMaxLength: draft.trainMaxLength,
        trainLearningRate: draft.trainLearningRate,
        trainWeightDecay: draft.trainWeightDecay,
        trainWarmupRatio: draft.trainWarmupRatio,
        trainDevice: draft.trainDevice,
      });
      pushLog("success", "Paramètres d'entraînement ML enregistrés.");
    } catch (error) {
      pushLog("error", `Échec de l'enregistrement ML : ${error instanceof Error ? error.message : "?"}`);
    }
  }, [draft, pushLog, updateAgentSettings]);

  const savePreferences = useCallback((value: string) => {
    setMaxHistorySize(value);
    pushLog("info", "Préférences appliquées.");
  }, [pushLog, setMaxHistorySize]);

  return (
    <>
      <header className="page-head"><h1>Paramètres</h1><p>Connexion à l'API ThinkTuning et préférences du dashboard.</p></header>
      <div className="page-body">
        <ApiConnectionSection draft={draft} updateDraft={updateDraft} configured={Boolean(config.apiKey)} onSubmit={saveConfig} />
        <AgentSettingsSection draft={draft} updateDraft={updateDraft} loading={agentLoading} error={agentError} onTest={testConnection} onSave={saveAgent} />
        <TrainingSettingsSection draft={draft} updateDraft={updateDraft} loading={agentLoading} onSave={saveTraining} />
        <PreferencesSection maxHistorySize={maxHistorySize} onSubmit={savePreferences} />
        <AboutSection />
      </div>
    </>
  );
}
