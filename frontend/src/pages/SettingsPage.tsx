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

  const saveAgent = useCallback(async () => {
    try {
      await updateAgentSettings({
        provider: draft.provider,
        model: draft.model,
        ollamaUrl: draft.ollamaUrl,
        openrouterUrl: draft.openrouterUrl,
        openrouterApiKey: draft.openrouterApiKey,
        hfUrl: draft.hfUrl,
        hfApiKey: draft.hfApiKey,
        lmStudioUrl: draft.lmStudioUrl,
        timeoutSeconds: draft.timeoutSeconds,
        contextLength: draft.contextLength,
        temperature: draft.temperature,
        sseFirstEventTimeout: draft.sseFirstEventTimeout,
        sseHeartbeat: draft.sseHeartbeat,
        maxLlmRounds: draft.maxLlmRounds,
        maxToolCalls: draft.maxToolCalls,
        logLevel: draft.logLevel,
        mcpFirst: draft.mcpFirst,
        mcpAuthRequired: draft.mcpAuthRequired,
        ssrfEnabled: draft.ssrfEnabled,
        ssrfAllowlist: draft.ssrfAllowlist,
        flagReliability: draft.flagReliability,
        flagAudit: draft.flagAudit,
        flagToolAnalytics: draft.flagToolAnalytics,
        flagContext: draft.flagContext,
        flagCopilot: draft.flagCopilot,
        flagWebsocket: draft.flagWebsocket,
        flagMultiAgent: draft.flagMultiAgent,
        flagCustomTools: draft.flagCustomTools,
        flagNewCore: draft.flagNewCore,
        flagLlmV2: draft.flagLlmV2,
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
