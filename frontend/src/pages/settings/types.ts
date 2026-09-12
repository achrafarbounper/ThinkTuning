import type { AgentSettings } from "../../api/agentSettings";

export type DraftShape = {
  baseUrl: string;
  apiKey: string;
  [key: string]: string | number | boolean;
} & Omit<AgentSettings, "timeoutSeconds" | "contextLength" | "temperature" | "trainMaxPerLang" | "trainAugmentFraction" | "trainVariantsPerExample" | "trainEpochs" | "trainBatchSize" | "trainNumWorkers" | "trainMaxLength" | "trainLearningRate" | "trainWeightDecay" | "trainWarmupRatio" | "maxLlmRounds" | "maxToolCalls"> & {
  timeoutSeconds: number | string;
  contextLength: number | string;
  temperature: number | string;
  trainMaxPerLang: number | string;
  trainAugmentFraction: number | string;
  trainVariantsPerExample: number | string;
  trainEpochs: number | string;
  trainBatchSize: number | string;
  trainNumWorkers: number | string;
  trainMaxLength: number | string;
  trainLearningRate: number | string;
  trainWeightDecay: number | string;
  trainWarmupRatio: number | string;
  maxLlmRounds: number | string;
  maxToolCalls: number | string;
};

export type NumericDraftKey =
  | "timeoutSeconds"
  | "contextLength"
  | "temperature"
  | "trainMaxPerLang"
  | "trainAugmentFraction"
  | "trainVariantsPerExample"
  | "trainEpochs"
  | "trainBatchSize"
  | "trainNumWorkers"
  | "trainMaxLength"
  | "trainLearningRate"
  | "trainWeightDecay"
  | "trainWarmupRatio"
  | "maxLlmRounds"
  | "maxToolCalls";

export type BooleanDraftKey =
  | "trainUseBackTranslation"
  | "mcpFirst"
  | "mcpAuthRequired"
  | "ssrfEnabled"
  | "flagReliability"
  | "flagAudit"
  | "flagToolAnalytics"
  | "flagContext"
  | "flagCopilot"
  | "flagWebsocket"
  | "flagMultiAgent"
  | "flagCustomTools"
  | "flagNewCore"
  | "flagLlmV2";

export const AGENT_FLAG_LABELS: Array<{ key: BooleanDraftKey; label: string; hint: string }> = [
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

export function createDraft(config: { baseUrl: string; apiKey: string }, settings: AgentSettings): DraftShape {
  return {
    baseUrl: config.baseUrl,
    apiKey: config.apiKey,
    ...settings,
  } as DraftShape;
}
