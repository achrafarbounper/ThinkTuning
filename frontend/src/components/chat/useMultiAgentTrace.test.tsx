/**
 * Tests du hook `useMultiAgentTrace` (L3 SCRUM-154) : reducer + persistance
 * localStorage (restauration au montage, écriture à chaque changement).
 */
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { MULTI_AGENT_TRACE_STORAGE_KEY, useMultiAgentTrace } from "./useMultiAgentTrace";

describe("useMultiAgentTrace", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("démarre sur la trace vide puis absorbe les événements MCP", () => {
    const { result } = renderHook(() => useMultiAgentTrace());
    expect(result.current.trace).toEqual({});

    act(() => {
      result.current.handleMcpEvent({
        kind: "started",
        run_id: "run-1",
        resumed: false,
        last_sequence: 4,
      });
      result.current.handleMcpEvent({
        kind: "intent",
        intent: "diagnostic",
        payload: {},
      });
    });

    expect(result.current.trace).toMatchObject({ runId: "run-1", intent: "diagnostic" });
    // Persistance best-effort : l'état est réécrit à chaque changement.
    expect(window.localStorage.getItem(MULTI_AGENT_TRACE_STORAGE_KEY)).toContain('"runId":"run-1"');
  });

  it("restaure la trace persistée au montage (survit au rechargement)", () => {
    window.localStorage.setItem(
      MULTI_AGENT_TRACE_STORAGE_KEY,
      JSON.stringify({ runId: "run-9", lastSequence: 12, workers: [] }),
    );

    const { result } = renderHook(() => useMultiAgentTrace());
    expect(result.current.trace).toEqual({ runId: "run-9", lastSequence: 12, workers: [] });
  });

  it("reset vide la trace (nouveau tour) et efface la persistance", () => {
    const { result } = renderHook(() => useMultiAgentTrace());
    act(() => {
      result.current.handleMcpEvent({
        kind: "started",
        run_id: "run-1",
        resumed: false,
        last_sequence: 0,
      });
    });
    expect(result.current.trace.runId).toBe("run-1");

    act(() => {
      result.current.reset();
    });
    expect(result.current.trace).toEqual({});
    expect(window.localStorage.getItem(MULTI_AGENT_TRACE_STORAGE_KEY)).toBeNull();
  });

  it("hydrate restaure explicitement une trace (restauration de session)", () => {
    const { result } = renderHook(() => useMultiAgentTrace());
    act(() => {
      result.current.hydrate({ runId: "run-2", tools: [{ tool: "now", event: "tool_start" }] });
    });
    expect(result.current.trace).toMatchObject({
      runId: "run-2",
      tools: [{ tool: "now", event: "tool_start" }],
    });
  });
});
