/**
 * Tests du reducer pur `applyMcpEvent` (mcpTrace.ts — L3 SCRUM-154) :
 * immutabilité, tolérance aux payloads partiels et CRITÈRE « la trace n'est
 * JAMAIS effacée silencieusement ».
 */
import { describe, expect, it } from "vitest";
import {
  applyMcpEvent,
  deserializeTrace,
  EMPTY_MULTI_AGENT_TRACE,
  serializeTrace,
} from "./mcpTrace";

describe("applyMcpEvent", () => {
  it("mémorise le run_id durable et le curseur (started)", () => {
    const state = applyMcpEvent(EMPTY_MULTI_AGENT_TRACE, {
      kind: "started",
      run_id: "run-1",
      resumed: false,
      last_sequence: 5,
    });
    expect(state).toEqual({ runId: "run-1", lastSequence: 5 });
  });

  it("ne régresse JAMAIS sur un run_id connu (started sans run_id)", () => {
    const state = applyMcpEvent(EMPTY_MULTI_AGENT_TRACE, {
      kind: "started",
      run_id: "run-1",
      resumed: false,
      last_sequence: 2,
    });
    const next = applyMcpEvent(state, {
      kind: "started",
      run_id: null,
      resumed: false,
      last_sequence: 0,
    });
    expect(next).toBe(state); // même objet : rien ne régresse
  });

  it("absorbe plan / workers / réflexion via multi_agent", () => {
    let state = applyMcpEvent(EMPTY_MULTI_AGENT_TRACE, {
      kind: "multi_agent",
      event: "agent.plan",
      payload: { event: "agent.plan", plan: [{ task_id: "t1", role: "ops", subtask: "Vérifier" }] },
    });
    state = applyMcpEvent(state, {
      kind: "multi_agent",
      event: "agent.worker.result",
      payload: {
        event: "agent.worker.result",
        task_id: "t1",
        role: "ops",
        status: "ok",
        summary: "Terminé.",
        duration_ms: 1200,
      },
    });
    expect(state.plan).toHaveLength(1);
    expect(state.workers).toEqual([
      { task_id: "t1", role: "ops", status: "ok", summary: "Terminé.", durationMs: 1200 },
    ]);
  });

  it("mémorise les approbations HITL (agent.worker.approval)", () => {
    const state = applyMcpEvent(EMPTY_MULTI_AGENT_TRACE, {
      kind: "multi_agent",
      event: "agent.worker.approval",
      payload: {
        event: "agent.worker.approval",
        task_id: "t2",
        status: "awaiting_approval",
        request_id: "req-9",
        approval: { tool: "write_file", reason: "écriture", args: { path: "x" } },
      },
    });
    expect(state.approvals).toEqual([
      { request_id: "req-9", tool: "write_file", reason: "écriture", args: { path: "x" }, task_id: "t2" },
    ]);
    expect(state.workers?.[0]?.status).toBe("awaiting_approval");
  });

  it("mémorise intent et skipped, les outils, la notice de repli et l'erreur", () => {
    let state = applyMcpEvent(EMPTY_MULTI_AGENT_TRACE, {
      kind: "intent",
      intent: "analyse",
      payload: {},
    });
    state = applyMcpEvent(state, {
      kind: "skipped",
      worker_id: "w9",
      reason: "hors périmètre",
      payload: {},
    });
    state = applyMcpEvent(state, {
      kind: "tool",
      tool: { event: "tool_start", tool: "now", args: {} },
    });
    state = applyMcpEvent(state, { kind: "fallback", reason: "capacité", payload: {} });
    state = applyMcpEvent(state, { kind: "error", event: "agent.error", message: "Boom", payload: {} });
    expect(state.intent).toBe("analyse");
    expect(state.skipped).toEqual([{ worker_id: "w9", reason: "hors périmètre" }]);
    expect(state.tools?.[0]).toMatchObject({ tool: "now", event: "tool_start" });
    expect(state.notice).toContain("mono-agent");
    expect(state.error).toBe("Boom");
  });

  it("affiche la notice de phase en échec et la réponse finale (done)", () => {
    let state = applyMcpEvent(EMPTY_MULTI_AGENT_TRACE, {
      kind: "phase",
      status: "timeout",
      reason: "synthesis_timeout",
      payload: {},
    });
    state = applyMcpEvent(state, {
      kind: "done",
      payload: { event: "agent.done", final_answer: "Réponse finale." },
    });
    expect(state.notice).toContain("La synthèse a dépassé son délai");
    expect(state.finalAnswer).toBe("Réponse finale.");
  });

  it("ignore thinking et rpc (surfaces autres que la trace) sans mutation", () => {
    const thinking = applyMcpEvent(EMPTY_MULTI_AGENT_TRACE, { kind: "thinking", delta: "x" });
    const rpc = applyMcpEvent(EMPTY_MULTI_AGENT_TRACE, { kind: "rpc", rpc: {} as never });
    expect(thinking).toBe(EMPTY_MULTI_AGENT_TRACE);
    expect(rpc).toBe(EMPTY_MULTI_AGENT_TRACE);
  });

  it("serialize/deserialize : aller-retour et repli sûr sur entrée invalide", () => {
    const state = applyMcpEvent(EMPTY_MULTI_AGENT_TRACE, {
      kind: "started",
      run_id: "run-7",
      resumed: true,
      last_sequence: 9,
    });
    const restored = deserializeTrace(serializeTrace(state));
    expect(restored).toEqual({ runId: "run-7", lastSequence: 9 });
    expect(deserializeTrace(null)).toEqual(EMPTY_MULTI_AGENT_TRACE);
    expect(deserializeTrace("not-json{")).toEqual(EMPTY_MULTI_AGENT_TRACE);
    expect(deserializeTrace('{"runId":"run-2"}')).toEqual({ runId: "run-2" });
  });
});