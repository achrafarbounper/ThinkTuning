import { useCallback, type FormEvent } from "react";
import { DEFAULT_BASE_URL } from "../api/sentimentApiClient";
import { useApp } from "../context/useApp";
import { AboutSection } from "./settings/AboutSection";
import { AgentSettingsSection } from "./settings/AgentSettingsSection";
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
      await updateAgentSettings(draft);
      pushLog("success", "Paramètres de l'assistant IA enregistrés.");
    } catch (error) {
      pushLog("error", `Échec de l'enregistrement : ${error instanceof Error ? error.message : "?"}`);
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
        <PreferencesSection maxHistorySize={maxHistorySize} onSubmit={savePreferences} />
        <AboutSection />
      </div>
    </>
  );
}
