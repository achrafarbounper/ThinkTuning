import { useState, type FormEvent, type ReactNode } from "react";
import {
  AGENT_FLAG_LABELS,
  type BooleanDraftKey,
  type DraftShape,
  type NumericDraftKey,
} from "./types";
import type { DraftUpdater } from "./useSettingsDraft";
import { useApp } from "../../context/useApp";
import type { AgentProviderDocument, AgentProviderInput } from "../../api/agentSettings";

interface Props {
  draft: DraftShape;
  updateDraft: DraftUpdater;
  loading: boolean;
  error: string | null;
  onTest: () => void;
  onSave: (overrides?: Partial<DraftShape>) => void | Promise<void>;
}

function Field({ id, label, value, onChange, type = "text", min, max, step, placeholder, disabled }: {
  id: string; label: string; value: string | number; onChange: (value: string) => void;
  type?: string; min?: number; max?: number; step?: number; placeholder?: string; disabled?: boolean;
}) {
  return (
    <label htmlFor={id}>
      <span className="tt-assistant-label">{label}</span>
      <input id={id} type={type} min={min} max={max} step={step} value={value}
        onChange={(event) => onChange(event.target.value)} placeholder={placeholder} disabled={disabled}
        className="tt-input-tt-settings" />
    </label>
  );
}

function Section({ title, children, help }: { title: string; children: ReactNode; help?: string }) {
  return <div className="tt-assistant-section"><h3 className="tt-assistant-label">{title}</h3>{help && <p className="tt-assistant-section-help">{help}</p>}{children}</div>;
}

function ModalSection({ title, children }: { title: string; children: ReactNode }) {
  return <fieldset className="tt-modal-section"><legend>{title}</legend>{children}</fieldset>;
}

function ModalCheck({ id, checked, onChange, children }: { id: string; checked: boolean; onChange: (value: boolean) => void; children: ReactNode }) {
  return <label className="tt-assistant-checkbox" htmlFor={id}><input id={id} type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />{children}</label>;
}

export function AgentSettingsSection({ draft, updateDraft, loading, error, onTest, onSave }: Props) {
  const { agentProviders, saveAgentProvider, deleteAgentProvider, activateAgentProvider, pushLog } = useApp();
  const [isProviderModalOpen, setProviderModalOpen] = useState(false);
  const [providerForm, setProviderForm] = useState<AgentProviderInput>({
    provider: { name: "", type: "hébergé", description: "", model_id: "openrouter/free",
      timeout_seconds: 600, context_length_tokens: 2048, temperature: 0.2,
      base_url: "https://openrouter.ai/api/v1", has_api_key: false, api_key_masked: "",
      streaming_sse: { first_event_seconds: 60, heartbeat_seconds: 10 } },
    assistant: { name: "Assistant IA", status: "prêt" },
    budgets: { max_llm_rounds_per_run: 6, max_tool_calls_per_run: 20 },
    logging: { level: "INFO" },
    mcp: { surface: "MCP-First", http_legacy_read_only: true, auth_required: true },
    network_security: { ssrf_protection_enabled: true, allowed_private_hosts: [] },
    features: {},
  });
  const isOllama = draft.provider === "ollama";
  const isHf = draft.provider === "hf";
  const isLmStudio = draft.provider === "lm_studio";
  const updateText = (key: Exclude<keyof DraftShape, NumericDraftKey | BooleanDraftKey>) =>
    (value: string) => updateDraft(key, value);
  const updateNumber = (key: NumericDraftKey) => (value: string) =>
    updateDraft(key, Number(value));
  const providerDescription = isOllama ? "Exécute les requêtes LLM locales via Docker Ollama." : isHf
    ? "Accède aux LLM via Hugging Face Inference Providers avec votre token HF."
    : isLmStudio ? "Serveur local LM Studio — démarrez le serveur (onglet Developer) ; aucune clé requise."
    : "Accède aux LLM via OpenRouter avec votre clé API.";
  const activeProvider = agentProviders.find((provider) =>
    provider.provider.model_id === draft.model && provider.provider.base_url === (draft.openrouterUrl || draft.ollamaUrl || draft.hfUrl || draft.lmStudioUrl)
  );
  const updateProviderForm = (field: string, value: string | number) =>
    setProviderForm((current) => ({
      ...current,
      provider: { ...current.provider, [field]: value },
    }));
  const updateProviderSection = <K extends Exclude<keyof AgentProviderDocument, "id" | "provider">>(
    section: K,
    field: string,
    value: string | number | boolean | string[],
  ) => setProviderForm((current) => ({
    ...current,
    [section]: { ...(current[section] as Record<string, unknown>), [field]: value },
  }));
  const updateFeature = (field: string, value: boolean | string | null) =>
    setProviderForm((current) => ({ ...current, features: { ...current.features, [field]: value } }));
  const submitProvider = async (event: FormEvent) => {
    event.preventDefault();
    try {
      const saved = await saveAgentProvider(providerForm);
      setProviderModalOpen(false);
      setProviderForm({ ...providerForm, id: undefined, provider: { ...providerForm.provider, name: "", api_key: undefined, has_api_key: false, api_key_masked: "" } });
      pushLog("success", `Provider « ${saved.provider.name} » enregistré.`);
    } catch (err) {
      pushLog("error", `Provider non enregistré : ${err instanceof Error ? err.message : String(err)}`);
    }
  };
  const activateProvider = async (provider: AgentProviderDocument) => {
    try {
      await activateAgentProvider(provider.id);
      updateDraft("provider", provider.provider.base_url.includes("openrouter") ? "openrouter" : draft.provider);
      updateDraft("model", provider.provider.model_id);
      updateDraft("timeoutSeconds", provider.provider.timeout_seconds);
      updateDraft("contextLength", provider.provider.context_length_tokens);
      updateDraft("temperature", provider.provider.temperature);
      updateDraft("openrouterUrl", provider.provider.base_url);
      pushLog("success", `Provider « ${provider.provider.name} » activé.`);
    } catch (err) {
      pushLog("error", `Provider non activé : ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  return (
    <section className="tt-panel tt-settings-assistant-panel" aria-labelledby="settings-agent-title">
      <div className="tt-panel-head"><h2 id="settings-agent-title">Assistant IA</h2>
        <span className={`tt-tag ${loading ? "tt-tag-status-pending" : "tt-tag-status-completed"}`}>{loading ? "sauvegarde..." : "prêt"}</span>
      </div>
      <div className="tt-provider-toolbar">
        <div><span className="tt-assistant-label">Provider actif</span><strong>{activeProvider?.provider.name ?? `${draft.provider} · ${draft.model || "modèle par défaut"}`}</strong></div>
        <button type="button" className="tt-btn tt-btn-primary" onClick={() => setProviderModalOpen(true)}>+ Ajouter un provider</button>
      </div>
      {agentProviders.length > 0 && <div className="tt-provider-list" aria-label="Providers enregistrés">
        {agentProviders.map((provider) => <div className={`tt-provider-card ${activeProvider?.id === provider.id ? "is-active" : ""}`} key={provider.id}>
          <div><strong>{provider.provider.name}</strong><span>{provider.provider.type} · {provider.provider.model_id}</span><small>{provider.provider.description || "Aucune description"}</small></div>
          <div className="tt-provider-card-actions">
            <button type="button" className="tt-btn tt-btn-ghost" onClick={() => void activateProvider(provider)}>{activeProvider?.id === provider.id ? "Actif" : "Activer"}</button>
            <button type="button" className="tt-btn tt-btn-ghost" aria-label={`Supprimer ${provider.provider.name}`} onClick={() => void deleteAgentProvider(provider.id).catch((err) => pushLog("error", err instanceof Error ? err.message : String(err)))}>Supprimer</button>
          </div>
        </div>)}
      </div>}
      <div className="tt-assistant-section">
        <label htmlFor="settings-provider"><span className="tt-assistant-label">Provider LLM</span></label>
        <select id="settings-provider" value={draft.provider} onChange={(event) => {
          const provider = event.target.value;
          updateDraft("provider", provider);
          if (provider === "openrouter") updateDraft("openrouterUrl", "https://openrouter.ai/api/v1");
          else if (provider === "hf") updateDraft("hfUrl", "https://router.huggingface.co/v1");
          else if (provider === "lm_studio") updateDraft("lmStudioUrl", "******");
          else updateDraft("ollamaUrl", "");
        }} className="tt-select-tt-settings">
          <option value="ollama">Ollama (local)</option><option value="openrouter">OpenRouter (hébergé)</option>
          <option value="hf">Hugging Face (Inference Providers)</option><option value="lm_studio">LM Studio (local)</option>
        </select>
      </div>
      <p className="tt-assistant-section-help">{providerDescription}</p>
      <div className="tt-assistant-grid">
        <Field id="settings-model" label="Modèle (ID)" value={draft.model} onChange={updateText("model")}
          placeholder={isOllama ? "qwen2.5:0.5b" : isLmStudio ? "modèle chargé dans LM Studio (vide = modèle actif)" : "vendor/openai/gpt-3.5-turbo"} />
        <Field id="settings-timeout" label="Timeout (s)" type="number" min={10} max={3600} value={draft.timeoutSeconds} onChange={updateNumber("timeoutSeconds")} />
        <Field id="settings-context" label="Context length (tokens)" type="number" min={512} max={131072} value={draft.contextLength} onChange={updateNumber("contextLength")} />
        <Field id="settings-temperature" label="Temperature (0.0-2.0)" type="number" min={0} max={2} step={0.1} value={draft.temperature} onChange={updateNumber("temperature")} />
      </div>
      {isOllama && <Field id="settings-ollama-url" label="URL Ollama" value={draft.ollamaUrl} onChange={updateText("ollamaUrl")} placeholder="http://localhost:11434" />}
      {!isOllama && !isHf && !isLmStudio && <>
        <Field id="settings-openrouter-url" label="URL OpenRouter" value={draft.openrouterUrl} onChange={updateText("openrouterUrl")} placeholder="https://openrouter.ai/api/v1" />
        <Field id="settings-openrouter-key" label="Clé API OpenRouter" type="password" value={draft.openrouterApiKey} onChange={updateText("openrouterApiKey")} placeholder="sk-or-xxxxxxxxxxxx" />
      </>}
      {isLmStudio && <Field id="settings-lm-url" label="URL LM Studio" value={draft.lmStudioUrl} onChange={updateText("lmStudioUrl")} placeholder="******" />}
      {isHf && <>
        <Field id="settings-hf-url" label="URL Hugging Face" value={draft.hfUrl} onChange={updateText("hfUrl")} placeholder="https://router.huggingface.co/v1" />
        <Field id="settings-hf-key" label="Token API Hugging Face" type="password" value={draft.hfApiKey} onChange={updateText("hfApiKey")} placeholder="hf_xxxxxxxxxxxxxxxx" />
      </>}

      <Section title="Streaming SSE" help="Délais de démarrage et de maintien du flux de l'assistant derrière les proxies.">
        <div className="tt-assistant-grid">
          <Field id="settings-sse-first-event-timeout" label="Premier événement (s)" type="number" min={1} max={300}
            value={draft.sseFirstEventTimeout} onChange={updateNumber("sseFirstEventTimeout")} />
          <Field id="settings-sse-heartbeat" label="Heartbeat (s)" type="number" min={1} max={120}
            value={draft.sseHeartbeat} onChange={updateNumber("sseHeartbeat")} />
        </div>
      </Section>
      <Section title="Budgets & garde-fous"><div className="tt-assistant-grid">
        <Field id="settings-rounds" label="Rounds LLM max par run" type="number" min={1} max={50} value={draft.maxLlmRounds} onChange={updateNumber("maxLlmRounds")} />
        <Field id="settings-tool-calls" label="Appels d'outils max par run" type="number" min={1} max={200} value={draft.maxToolCalls} onChange={updateNumber("maxToolCalls")} />
      </div></Section>
      <Section title="Niveau de log" help="Niveau des journaux du noyau agent (persisté dans la base).">
        <label htmlFor="settings-log-level"><span className="tt-assistant-label">Niveau de log</span></label>
        <select id="settings-log-level" value={draft.logLevel} onChange={(event) => updateDraft("logLevel", event.target.value)} className="tt-select-tt-settings"><option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option></select>
      </Section>
      <Section title="Surface MCP"><Check draft={draft} updateDraft={updateDraft} field="mcpFirst">MCP-First — surface HTTP legacy de l'agent en lecture seule</Check><Check draft={draft} updateDraft={updateDraft} field="mcpAuthRequired">Auth X-API-Key obligatoire sur le transport MCP</Check></Section>
      <Section title="Sécurité réseau (SSRF)"><Check draft={draft} updateDraft={updateDraft} field="ssrfEnabled">Protection SSRF — interdire les hôtes privés / loopback</Check>
        <Field id="settings-allowlist" label="Hôtes privés autorisés (CSV)" value={draft.ssrfAllowlist} disabled={!draft.ssrfEnabled} onChange={updateText("ssrfAllowlist")} placeholder="127.0.0.1,localhost,searxng" />
      </Section>
      <Section title="Fonctionnalités" help="Bascules persistées via le module de configuration IHM (base MongoDB) — appliquées lors du prochain run de l'agent.">
        {AGENT_FLAG_LABELS.map(({ key, label, hint }) => <Check key={key} draft={draft} updateDraft={updateDraft} field={key}><span>{label}</span><small className="tt-assistant-section-help">{hint}</small></Check>)}
      </Section>
      <div className="tt-assistant-actions">
        <button type="button" className="tt-btn tt-btn-ghost" onClick={onTest} disabled={loading}>{loading ? "Test..." : "Tester la connexion"}</button>
        <button type="button" className="tt-btn tt-btn-primary" onClick={() => void onSave()} disabled={loading}>{loading ? "Enregistrement..." : "Enregistrer"}</button>
      </div>
      {error && <div className="tt-alert tt-alert-error" role="alert">{error}</div>}
      {isProviderModalOpen && <div className="tt-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setProviderModalOpen(false); }}>
        <form className="tt-modal" onSubmit={submitProvider} aria-labelledby="provider-modal-title">
          <div className="tt-modal-head"><div><span className="tt-eyebrow">Configuration persistée</span><h2 id="provider-modal-title">Ajouter un provider</h2></div><button type="button" className="tt-modal-close" onClick={() => setProviderModalOpen(false)} aria-label="Fermer">×</button></div>
          <p className="tt-assistant-section-help">Chaque provider est enregistré comme un document MongoDB. La clé API est masquée après sauvegarde.</p>
          <ModalSection title="Assistant">
            <div className="tt-assistant-grid">
              <Field id="provider-assistant-name" label="Nom de l'assistant" value={providerForm.assistant.name} onChange={(value) => updateProviderSection("assistant", "name", value)} />
              <label htmlFor="provider-assistant-status"><span className="tt-assistant-label">Statut</span><select id="provider-assistant-status" className="tt-select-tt-settings" value={providerForm.assistant.status} onChange={(event) => updateProviderSection("assistant", "status", event.target.value)}><option>prêt</option><option>maintenance</option><option>désactivé</option></select></label>
            </div>
          </ModalSection>
          <ModalSection title="Provider">
            <div className="tt-assistant-grid">
              <Field id="provider-name" label="Nom" value={providerForm.provider.name} onChange={(value) => updateProviderForm("name", value)} placeholder="OpenRouter production" />
              <Field id="provider-type" label="Type" value={providerForm.provider.type} onChange={(value) => updateProviderForm("type", value)} placeholder="hébergé / local" />
              <Field id="provider-model" label="Model ID" value={providerForm.provider.model_id} onChange={(value) => updateProviderForm("model_id", value)} placeholder="openrouter/free" />
              <Field id="provider-url" label="Base URL" value={providerForm.provider.base_url} onChange={(value) => updateProviderForm("base_url", value)} placeholder="https://openrouter.ai/api/v1" />
              <Field id="provider-key" label="Clé API" type="password" value={providerForm.provider.api_key || ""} onChange={(value) => updateProviderForm("api_key", value)} placeholder="sk-or-..." />
              <Field id="provider-timeout" label="Timeout (s)" type="number" min={10} max={3600} value={providerForm.provider.timeout_seconds} onChange={(value) => updateProviderForm("timeout_seconds", Number(value))} />
              <Field id="provider-context" label="Contexte (tokens)" type="number" min={512} max={131072} value={providerForm.provider.context_length_tokens} onChange={(value) => updateProviderForm("context_length_tokens", Number(value))} />
              <Field id="provider-temperature" label="Temperature" type="number" min={0} max={2} step={0.1} value={providerForm.provider.temperature} onChange={(value) => updateProviderForm("temperature", Number(value))} />
              <Field id="provider-sse-first" label="Premier événement SSE (s)" type="number" min={1} max={300} value={providerForm.provider.streaming_sse.first_event_seconds} onChange={(value) => setProviderForm((current) => ({ ...current, provider: { ...current.provider, streaming_sse: { ...current.provider.streaming_sse, first_event_seconds: Number(value) } } }))} />
              <Field id="provider-sse-heartbeat" label="Heartbeat SSE (s)" type="number" min={1} max={120} value={providerForm.provider.streaming_sse.heartbeat_seconds} onChange={(value) => setProviderForm((current) => ({ ...current, provider: { ...current.provider, streaming_sse: { ...current.provider.streaming_sse, heartbeat_seconds: Number(value) } } }))} />
            </div>
            <label htmlFor="provider-description"><span className="tt-assistant-label">Description</span><textarea id="provider-description" value={providerForm.provider.description || ""} onChange={(event) => updateProviderForm("description", event.target.value)} rows={3} /></label>
          </ModalSection>
          <ModalSection title="Budgets">
            <div className="tt-assistant-grid">
              <Field id="provider-max-rounds" label="Rounds LLM max par run" type="number" min={1} max={50} value={providerForm.budgets.max_llm_rounds_per_run} onChange={(value) => updateProviderSection("budgets", "max_llm_rounds_per_run", Number(value))} />
              <Field id="provider-max-tools" label="Appels outils max par run" type="number" min={1} max={200} value={providerForm.budgets.max_tool_calls_per_run} onChange={(value) => updateProviderSection("budgets", "max_tool_calls_per_run", Number(value))} />
            </div>
          </ModalSection>
          <ModalSection title="Logging">
            <label htmlFor="provider-log-level"><span className="tt-assistant-label">Niveau de log</span><select id="provider-log-level" className="tt-select-tt-settings" value={providerForm.logging.level} onChange={(event) => updateProviderSection("logging", "level", event.target.value)}><option>DEBUG</option><option>INFO</option><option>WARN</option><option>WARNING</option><option>ERROR</option><option>TRACE</option></select></label>
          </ModalSection>
          <ModalSection title="MCP">
            <div className="tt-assistant-grid">
              <Field id="provider-mcp-surface" label="Surface" value={providerForm.mcp.surface} onChange={(value) => updateProviderSection("mcp", "surface", value)} placeholder="MCP-First" />
              <ModalCheck id="provider-mcp-readonly" checked={providerForm.mcp.http_legacy_read_only} onChange={(value) => updateProviderSection("mcp", "http_legacy_read_only", value)}>HTTP legacy read-only</ModalCheck>
              <ModalCheck id="provider-mcp-auth" checked={providerForm.mcp.auth_required} onChange={(value) => updateProviderSection("mcp", "auth_required", value)}>Authentification requise</ModalCheck>
            </div>
          </ModalSection>
          <ModalSection title="Sécurité réseau">
            <ModalCheck id="provider-ssrf" checked={providerForm.network_security.ssrf_protection_enabled} onChange={(value) => updateProviderSection("network_security", "ssrf_protection_enabled", value)}>Protection SSRF activée</ModalCheck>
            <label htmlFor="provider-private-hosts"><span className="tt-assistant-label">Hôtes privés autorisés (CSV)</span><textarea id="provider-private-hosts" value={providerForm.network_security.allowed_private_hosts.join(", ")} onChange={(event) => updateProviderSection("network_security", "allowed_private_hosts", event.target.value.split(",").map((host) => host.trim()).filter(Boolean))} rows={2} placeholder="127.0.0.1, localhost, searxng" /></label>
          </ModalSection>
          <ModalSection title="Fonctionnalités">
            <div className="tt-provider-feature-grid">
              <ModalCheck id="provider-feature-retry" checked={Boolean(providerForm.features.retry_and_circuit_breaker)} onChange={(value) => updateFeature("retry_and_circuit_breaker", value)}>Retry & circuit breaker</ModalCheck>
              <ModalCheck id="provider-feature-audit" checked={Boolean(providerForm.features.audit_logging)} onChange={(value) => updateFeature("audit_logging", value)}>Audit logging</ModalCheck>
              <ModalCheck id="provider-feature-tools" checked={Boolean(providerForm.features.tool_usage_stats)} onChange={(value) => updateFeature("tool_usage_stats", value)}>Statistiques outils</ModalCheck>
              <ModalCheck id="provider-feature-context" checked={Boolean(providerForm.features.context_management)} onChange={(value) => updateFeature("context_management", value)}>Gestion du contexte</ModalCheck>
              <ModalCheck id="provider-feature-copilot" checked={Boolean(providerForm.features.copilot_suggestions)} onChange={(value) => updateFeature("copilot_suggestions", value)}>Suggestions Copilot</ModalCheck>
              <ModalCheck id="provider-feature-websocket" checked={Boolean(providerForm.features.websocket_streaming)} onChange={(value) => updateFeature("websocket_streaming", value)}>Streaming WebSocket</ModalCheck>
              <ModalCheck id="provider-feature-multi" checked={Boolean(providerForm.features.multi_agents_orchestration)} onChange={(value) => updateFeature("multi_agents_orchestration", value)}>Orchestration multi-agents</ModalCheck>
              <ModalCheck id="provider-feature-core" checked={Boolean(providerForm.features.agent_core_v2)} onChange={(value) => updateFeature("agent_core_v2", value)}>Agent core v2</ModalCheck>
              <ModalCheck id="provider-feature-llm" checked={Boolean(providerForm.features.llm_client_v2)} onChange={(value) => updateFeature("llm_client_v2", value)}>Client LLM v2</ModalCheck>
            </div>
            <Field id="provider-custom-tools" label="Registre custom tools" value={String(providerForm.features.custom_tools_registry ?? "")} onChange={(value) => updateFeature("custom_tools_registry", value || null)} placeholder="SCRUM-99" />
          </ModalSection>
          <div className="tt-assistant-actions"><button type="button" className="tt-btn tt-btn-ghost" onClick={() => setProviderModalOpen(false)}>Annuler</button><button type="submit" className="tt-btn tt-btn-primary">Enregistrer le provider</button></div>
        </form>
      </div>}
    </section>
  );
}

function Check({ draft, updateDraft, field, children }: { draft: DraftShape; updateDraft: DraftUpdater; field: BooleanDraftKey; children: ReactNode }) {
  return <label className="tt-assistant-checkbox"><input type="checkbox" checked={draft[field]} onChange={(event) => updateDraft(field, event.target.checked)} />{children}</label>;
}
