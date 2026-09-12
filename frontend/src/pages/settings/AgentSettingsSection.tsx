import type { ReactNode } from "react";
import {
  AGENT_FLAG_LABELS,
  type BooleanDraftKey,
  type DraftShape,
  type NumericDraftKey,
} from "./types";
import type { DraftUpdater } from "./useSettingsDraft";

interface Props {
  draft: DraftShape;
  updateDraft: DraftUpdater;
  loading: boolean;
  error: string | null;
  onTest: () => void;
  onSave: () => void;
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

export function AgentSettingsSection({ draft, updateDraft, loading, error, onTest, onSave }: Props) {
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

  return (
    <section className="tt-panel tt-settings-assistant-panel" aria-labelledby="settings-agent-title">
      <div className="tt-panel-head"><h2 id="settings-agent-title">Assistant IA</h2>
        <span className={`tt-tag ${loading ? "tt-tag-status-pending" : "tt-tag-status-completed"}`}>{loading ? "sauvegarde..." : "prêt"}</span>
      </div>
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
        <button type="button" className="tt-btn tt-btn-primary" onClick={onSave} disabled={loading}>{loading ? "Enregistrement..." : "Enregistrer"}</button>
      </div>
      {error && <div className="tt-alert tt-alert-error" role="alert">{error}</div>}
    </section>
  );
}

function Check({ draft, updateDraft, field, children }: { draft: DraftShape; updateDraft: DraftUpdater; field: BooleanDraftKey; children: ReactNode }) {
  return <label className="tt-assistant-checkbox"><input type="checkbox" checked={draft[field]} onChange={(event) => updateDraft(field, event.target.checked)} />{children}</label>;
}
