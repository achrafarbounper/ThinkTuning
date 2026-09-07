/**
 * ledger.test.ts — Contrat du « Journal des outils » (Tool Ledger).
 *
 * Verrouille :
 *  - l'ordre d'appel (seq 1..N, chronologie de la timeline) ;
 *  - la relation avec les agents (role → nœud du graphe) ;
 *  - le lien vers les arcs réels du graphe (sélection croisée) ;
 *  - l'appariement start/result (statut, durée, sortie, fin) ;
 *  - les appels restés ouverts (running) et les résultats orphelins ;
 *  - le déterminisme (deux réductions → même journal).
 */

import { describe, expect, it } from "vitest";

import { applyEvent, initGraph, reduceTimeline } from "./buildGraph";
import { demoTimeline } from "./demo";
import { buildToolLedger } from "./ledger";

describe("buildToolLedger — ordre d'appel et relation aux agents", () => {
  it("numérote les appels 1..N dans l'ordre de la timeline", () => {
    const g = reduceTimeline(demoTimeline());
    const ledger = buildToolLedger(g);
    expect(ledger.length).toBe(g.toolCalls);
    expect(ledger.map((r) => r.seq)).toEqual(ledger.map((_, i) => i + 1));
    expect(ledger.map((r) => r.tool)).toEqual([
      "web_search",
      "http_get",
      "read_file",
      "run_python",
      "run_python",
      "write_file",
      "write_file",
    ]);
  });

  it("rattache chaque appel à l'agent exécutant (nœud du graphe)", () => {
    const g = reduceTimeline(demoTimeline());
    const ledger = buildToolLedger(g);
    for (const r of ledger) {
      expect(g.nodes[`role:${r.role}`]).toBeDefined();
    }
    expect(ledger[0]).toMatchObject({ role: "web_search", tool: "web_search" });
    expect(ledger[3]).toMatchObject({ role: "code_analysis", tool: "run_python" });
  });

  it("pointe vers des arcs réels du graphe (départ, et résultat si clos)", () => {
    const g = reduceTimeline(demoTimeline());
    const ledger = buildToolLedger(g);
    for (const r of ledger) {
      expect(g.edges[r.startEdgeId]).toBeDefined();
      expect(g.edges[r.startEdgeId].kind).toBe("tool");
      if (r.resultEdgeId) {
        expect(g.edges[r.resultEdgeId]).toBeDefined();
        expect(g.edges[r.resultEdgeId].status).toBe(r.status);
      }
    }
  });
});

describe("buildToolLedger — métadonnées d'appel", () => {
  it("referme chaque appel : statut, durée, sortie et instant de fin", () => {
    const ledger = buildToolLedger(reduceTimeline(demoTimeline()));
    const first = ledger[0];
    expect(first.status).toBe("ok");
    expect(first.durationMs).toBe(118);
    expect(first.summary).toBe("12 résultats pertinents.");
    expect(first.endedAt).toBeGreaterThan(first.startedAt);
    expect(first.args).toBe('{"query":"dérive climatique sources françaises"}');
    const last = ledger[ledger.length - 1];
    expect(last.status).toBe("error");
    expect(last.summary).toContain("Permission refusée");
  });

  it("laisse ouvert (running) un appel sans tool.result", () => {
    const g = initGraph();
    applyEvent(g, { t: "worker.start", at: 0, task_id: "t1", role: "web_search" });
    applyEvent(g, {
      t: "tool.start",
      at: 5,
      task_id: "t1",
      role: "web_search",
      tool: "http_get",
      args: '{"url":"https://x"}',
    });
    const ledger = buildToolLedger(g);
    expect(ledger).toHaveLength(1);
    expect(ledger[0].status).toBe("running");
    expect(ledger[0].args).toBe('{"url":"https://x"}');
    expect(ledger[0].summary).toBeUndefined();
    expect(ledger[0].resultEdgeId).toBeUndefined();
  });

  it("apparie chaque résultat au plus ancien appel ouvert du même (tâche, agent, outil)", () => {
    const g = initGraph();
    applyEvent(g, { t: "tool.start", at: 1, task_id: "t1", role: "web_search", tool: "http_get" });
    applyEvent(g, { t: "tool.start", at: 2, task_id: "t2", role: "web_search", tool: "http_get" });
    applyEvent(g, {
      t: "tool.result",
      at: 3,
      task_id: "t1",
      role: "web_search",
      tool: "http_get",
      status: "ok",
      duration_ms: 10,
    });
    applyEvent(g, {
      t: "tool.result",
      at: 4,
      task_id: "t2",
      role: "web_search",
      tool: "http_get",
      status: "error",
      duration_ms: 20,
    });
    const ledger = buildToolLedger(g);
    expect(ledger).toHaveLength(2);
    expect(ledger[0]).toMatchObject({ taskId: "t1", status: "ok" });
    expect(ledger[1]).toMatchObject({ taskId: "t2", status: "error" });
  });

  it("crée une entrée autonome pour un tool.result orphelin (timeline tronquée)", () => {
    const g = initGraph();
    applyEvent(g, {
      t: "tool.result",
      at: 9,
      task_id: "t9",
      role: "web_search",
      tool: "http_get",
      status: "ok",
      summary: "OK",
      duration_ms: 12,
    });
    const ledger = buildToolLedger(g);
    expect(ledger).toHaveLength(1);
    expect(ledger[0]).toMatchObject({
      taskId: "t9",
      role: "web_search",
      tool: "http_get",
      status: "ok",
    });
    expect(ledger[0].startEdgeId).toBe("tool:web_search:http_get:1");
    expect(ledger[0].resultEdgeId).toBe("tool:web_search:http_get:1");
  });
});

describe("buildToolLedger — déterminisme", () => {
  it("deux réductions de la même timeline produisent le même journal", () => {
    const events = demoTimeline();
    expect(buildToolLedger(reduceTimeline(events))).toEqual(buildToolLedger(reduceTimeline(events)));
  });
});
