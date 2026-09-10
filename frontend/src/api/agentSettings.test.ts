/**
 * Tests des helpers de paramètres agent (module de configuration IHM).
 *
 * SCRUM-138 : budgets, niveau de log, surface MCP et feature flags ont été
 * déplacés de app/config/settings.py vers le module IHM (stockés/chargés
 * depuis MongoDB). Ces tests verrouillent le mapping camelCase ↔ snake_case
 * (agentSettingsPayload) et la normalisation API → UI (normalizeAgentSettings).
 */

import { describe, expect, it } from "vitest";
import {
  AGENT_FLAGS,
  AGENT_LOG_LEVELS,
  AGENT_MAX_LLM_ROUNDS_DEFAULT,
  AGENT_MAX_TOOL_CALLS_DEFAULT,
  agentFlagCamelCase,
  agentSettingsPayload,
  normalizeAgentSettings,
} from "./agentSettings";

describe("agentSettingsPayload (mapping camelCase → snake_case)", () => {
  it("mappe les budgets, le log level, MCP et les flags vers le contrat API", () => {
    const payload = agentSettingsPayload({
      maxLlmRounds: 8,
      maxToolCalls: 42,
      logLevel: "DEBUG",
      mcpFirst: true,
      mcpAuthRequired: false,
      flagReliability: false,
      flagAudit: true,
      flagContext: false,
      flagNewCore: true,
      flagLlmV2: false,
    });

    expect(payload.max_llm_rounds).toBe(8);
    expect(payload.max_tool_calls).toBe(42);
    expect(payload.log_level).toBe("DEBUG");
    expect(payload.mcp_first).toBe(true);
    expect(payload.mcp_auth_required).toBe(false);
    expect(payload.flag_reliability).toBe(false);
    expect(payload.flag_audit).toBe(true);
    expect(payload.flag_context).toBe(false);
    expect(payload.flag_new_core).toBe(true);
    expect(payload.flag_llm_v2).toBe(false);
  });

  it("n'envoie que les clés présentes (mise à jour partielle)", () => {
    const payload = agentSettingsPayload({ maxLlmRounds: 10 });
    expect(payload.max_llm_rounds).toBe(10);
    for (const name of AGENT_FLAGS) {
      expect(payload[`flag_${name}` as keyof typeof payload]).toBeUndefined();
    }
    expect(payload.mcp_first).toBeUndefined();
    expect(payload.mcp_auth_required).toBeUndefined();
  });

  it("n'envoie pas les booléens non fournis, mais envoie false explicitement", () => {
    const payload = agentSettingsPayload({ mcpFirst: false, mcpAuthRequired: false });
    expect(payload.mcp_first).toBe(false);
    expect(payload.mcp_auth_required).toBe(false);
  });
});
describe("normalizeAgentSettings (snake_case API → camelCase UI)", () => {
  it("restaure budgets, log level, MCP et flags depuis le payload serveur", () => {
    const settings = normalizeAgentSettings({
      provider: "ollama",
      model: "qwen2.5:0.5b",
      max_llm_rounds: 8,
      max_tool_calls: 42,
      log_level: "WARNING",
      mcp_first: true,
      mcp_auth_required: false,
      flag_reliability: false,
      flag_audit: true,
      flag_tool_analytics: false,
      flag_context: true,
      flag_copilot: false,
      flag_websocket: true,
      flag_multi_agent: false,
      flag_custom_tools: true,
      flag_new_core: false,
      flag_llm_v2: true,
    });

    expect(settings.maxLlmRounds).toBe(8);
    expect(settings.maxToolCalls).toBe(42);
    expect(settings.logLevel).toBe("WARNING");
    expect(settings.mcpFirst).toBe(true);
    expect(settings.mcpAuthRequired).toBe(false);
    expect(settings.flagReliability).toBe(false);
    expect(settings.flagAudit).toBe(true);
    expect(settings.flagToolAnalytics).toBe(false);
    expect(settings.flagContext).toBe(true);
    expect(settings.flagCopilot).toBe(false);
    expect(settings.flagWebsocket).toBe(true);
    expect(settings.flagMultiAgent).toBe(false);
    expect(settings.flagCustomTools).toBe(true);
    expect(settings.flagNewCore).toBe(false);
    expect(settings.flagLlmV2).toBe(true);
  });

  it("accepte aussi les clés camelCase (formulaire / localStorage)", () => {
    const settings = normalizeAgentSettings({
      maxLlmRounds: 3,
      maxToolCalls: 7,
      logLevel: "ERROR",
      mcpFirst: true,
      flagLlmV2: false,
    });
    expect(settings.maxLlmRounds).toBe(3);
    expect(settings.maxToolCalls).toBe(7);
    expect(settings.logLevel).toBe("ERROR");
    expect(settings.mcpFirst).toBe(true);
    expect(settings.flagLlmV2).toBe(false);
  });

  it("applique les défauts historiques (base absente ou clé manquante)", () => {
    const settings = normalizeAgentSettings(undefined);
    expect(settings.maxLlmRounds).toBe(AGENT_MAX_LLM_ROUNDS_DEFAULT);
    expect(settings.maxToolCalls).toBe(AGENT_MAX_TOOL_CALLS_DEFAULT);
    expect(AGENT_LOG_LEVELS).toContain(settings.logLevel);
    expect(settings.mcpFirst).toBe(false);
    expect(settings.mcpAuthRequired).toBe(true);
    for (const name of AGENT_FLAGS) {
      const key = agentFlagCamelCase(name) as keyof typeof settings;
      expect(settings[key]).toBe(true);
    }
  });

  it("normalise false venant du serveur (0/false ne sont pas écrasés par le défaut)", () => {
    const settings = normalizeAgentSettings({ flag_audit: false, mcp_auth_required: false });
    expect(settings.flagAudit).toBe(false);
    expect(settings.mcpAuthRequired).toBe(false);
  });
});

describe("round-trip payload ↔ normalize", () => {
  it("conserve budgets/level/MCP/flags à travers le cycle complet", () => {
    const draft = {
      provider: "openrouter",
      model: "anthropic/claude-3.5-sonnet",
      maxLlmRounds: 12,
      maxToolCalls: 60,
      logLevel: "INFO",
      mcpFirst: true,
      mcpAuthRequired: true,
      flagReliability: true,
      flagAudit: false,
      flagCustomTools: true,
      flagNewCore: true,
      flagLlmV2: true,
    };
    const normalized = normalizeAgentSettings(agentSettingsPayload(draft));
    expect(normalized.maxLlmRounds).toBe(12);
    expect(normalized.maxToolCalls).toBe(60);
    expect(normalized.logLevel).toBe("INFO");
    expect(normalized.mcpFirst).toBe(true);
    expect(normalized.mcpAuthRequired).toBe(true);
    expect(normalized.flagReliability).toBe(true);
    expect(normalized.flagAudit).toBe(false);
    expect(normalized.flagCustomTools).toBe(true);
    expect(normalized.flagNewCore).toBe(true);
    expect(normalized.flagLlmV2).toBe(true);
  });
});