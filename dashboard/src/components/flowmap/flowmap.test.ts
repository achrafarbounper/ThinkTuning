/**
 * flowmap.test.ts — Contrat de bout en bout du « Agent Flow Map ».
 *
 * Verrouille les invariants FSM du graphe :
 *  - un run `awaiting_approval` n'est JAMAIS affiché « completed » ;
 *  - la reprise native (`resuming`) est restituée et re-visible ;
 *  - le statut canonique est « awaiting_approval » (aligné flow_store) ;
 *  - l'approbation (outil / raison / empreinte SHA-256) est conservée ;
 *  - le replay des sessions persistées traduit chaque SSE sans perte.
 */

import { describe, expect, it } from "vitest";

import { applyEvent, initGraph, reduceTimeline } from "./buildGraph";
import { sseToFlowEvent, type SseFrame } from "./events";
import { demoTimeline } from "./demo";
import type { FlowEvent } from "./types";

const noSub = new Map<string, string>();

/** Construit une trame SSE nominative simulant l'émission de l'orchestrateur. */
function frame(event: string, data: Record<string, unknown>): SseFrame {
  return { event, data: JSON.stringify(data) };
}

/** Réduit une liste d'événements et retourne l'état final du graphe. */
function reduce(events: FlowEvent[]) {
  const g = initGraph();
  for (const e of events) applyEvent(g, e);
  return g;
}

describe("sseToFlowEvent — traduction des trames SSE", () => {
  it("agent.resuming est restitué (FAIBLESSE F1)", () => {
    const ev = sseToFlowEvent(
      frame("agent.resuming", { request_id: "req-7", task_id: "t3", role: "data_extractor" }),
      1_250,
      noSub,
    );
    expect(ev).toEqual({
      t: "resuming",
      at: 1_250,
      request_id: "req-7",
      task_id: "t3",
      role: "data_extractor",
    });
  });

  it("agent.done propage le statut réel (FAIBLESSE F4)", () => {
    const ev = sseToFlowEvent(
      frame("agent.done", {
        status: "awaiting_approval",
        final_answer: "Validation humaine requise.",
        duration_ms: 42,
      }),
      10,
      noSub,
    );
    expect(ev).toEqual({
      t: "done",
      at: 10,
      status: "awaiting_approval",
      answer: "Validation humaine requise.",
      duration_ms: 42,
    });
  });

  it("agent.worker.approval conserve outil, raison et empreinte SHA-256 (FAIBLESSE F6)", () => {
    const ev = sseToFlowEvent(
      frame("agent.worker.approval", {
        task_id: "t3",
        role: "data_extractor",
        status: "awaiting_approval",
        request_id: "req-7",
        message: "Écriture hors sandbox.",
        approval: {
          tool: "write_file",
          reason: "Validation requise.",
          args_hash: "abc123",
        },
      }),
      5,
      noSub,
    );
    expect(ev).toMatchObject({
      t: "worker.approval",
      task_id: "t3",
      role: "data_extractor",
      request_id: "req-7",
      approval: { tool: "write_file", reason: "Validation requise.", args_hash: "abc123" },
    });
  });
});
describe("applyEvent — réduction fidèle à la FSM", () => {
  it("worker.approval → nœud « awaiting » et statut canonique « awaiting_approval » (F2/F3)", () => {
    const g = initGraph();
    applyEvent(g, {
      t: "worker.approval",
      at: 1,
      task_id: "t3",
      role: "data_extractor",
      request_id: "req-7",
      approval: { tool: "write_file", reason: "Validation requise.", args_hash: "abc123" },
    });
    const node = g.nodes["role:data_extractor"];
    expect(node).toBeDefined();
    expect(node!.status).toBe("awaiting");
    expect(node!.pendingApproval).toMatchObject({
      tool: "write_file",
      reason: "Validation requise.",
      args_hash: "abc123",
      request_id: "req-7",
    });
    expect(g.runStatus).toBe("awaiting_approval");
  });

  it("resuming → runStatus « resuming », nœud en « running », attente effacée", () => {
    const events: FlowEvent[] = [
      {
        t: "worker.approval",
        at: 1,
        task_id: "t3",
        role: "data_extractor",
        request_id: "req-7",
        approval: { tool: "write_file" },
      },
      { t: "resuming", at: 2, request_id: "req-7", task_id: "t3", role: "data_extractor" },
    ];
    const g = reduce(events);
    expect(g.runStatus).toBe("resuming");
    const node = g.nodes["role:data_extractor"];
    expect(node!.status).toBe("running");
    expect(node!.pendingApproval).toBeUndefined();
  });

  it("done avec status awaiting_approval ne devient JAMAIS completed (F4)", () => {
    const g = reduce([
      {
        t: "worker.approval",
        at: 1,
        task_id: "t3",
        role: "data_extractor",
        request_id: "req-7",
      },
      { t: "resuming", at: 2, request_id: "req-7", task_id: "t3", role: "data_extractor" },
      {
        t: "done",
        at: 3,
        status: "awaiting_approval",
        answer: "En attente de validation humaine.",
      },
    ]);
    expect(g.runStatus).toBe("awaiting_approval");
    expect(g.finalAnswer).toBe("En attente de validation humaine.");
  });

  it("done sans status → completed (rétro-compatibilité)", () => {
    const g = reduce([
      { t: "worker.start", at: 1, task_id: "t1", role: "web_search" },
      { t: "worker.result", at: 2, task_id: "t1", role: "web_search" },
      { t: "done", at: 3, answer: "Terminé." },
    ]);
    expect(g.runStatus).toBe("completed");
  });

  it("la reprise re-dispatch le même worker (worker.start efface l'attente)", () => {
    const g = reduce([
      {
        t: "worker.approval",
        at: 1,
        task_id: "t3",
        role: "data_extractor",
        request_id: "req-7",
      },
      { t: "resuming", at: 2, request_id: "req-7", task_id: "t3", role: "data_extractor" },
      { t: "worker.start", at: 3, task_id: "t3", role: "data_extractor" },
      {
        t: "tool.start",
        at: 4,
        task_id: "t3",
        role: "data_extractor",
        tool: "write_file",
      },
    ]);
    const node = g.nodes["role:data_extractor"];
    expect(node!.calls).toBe(1);
    expect(node!.status).toBe("running");
  });
});

describe("demoTimeline — couvre le cycle approval → resume → synthèse", () => {
  it("rétablit un run complet avec reprise native", () => {
    const events = demoTimeline();
    const withResume = events.filter((e) => e.t === "resuming");
    expect(withResume).toHaveLength(1);
    const g = reduce(events);
    expect(g.runStatus).toBe("completed");
    // Le worker repris est bien revenu en « ok » après sa re-exécution.
    expect(g.nodes["role:data_extractor"]!.status).toBe("ok");
    // Aucun nœud ne reste en attente en fin de session.
    expect(Object.values(g.nodes).some((n) => n.status === "awaiting")).toBe(false);
  });

  it("le path approval → resume apparaît dans la timeline (replay)", () => {
    const events = demoTimeline();
    const iApproval = events.findIndex((e) => e.t === "worker.approval");
    const iResume = events.findIndex((e) => e.t === "resuming");
    expect(iApproval).toBeGreaterThanOrEqual(0);
    expect(iResume).toBeGreaterThan(iApproval);
    expect(iResume).toBeLessThan(events.findIndex((e) => e.t === "synthesizing"));
  });
});

describe("replay des sessions persistées (replay path)", () => {
  it("rejoue une timeline stockée avec agent.resuming via reduceTimeline", () => {
    const stored: SseFrame[] = [
      frame("agent.plan", {
        plan: [{ task_id: "t3", role: "data_extractor", subtask: "Écrire le fichier" }],
      }),
      frame("agent.worker.start", { task_id: "t3", role: "data_extractor" }),
      frame("agent.worker.approval", {
        task_id: "t3",
        role: "data_extractor",
        status: "awaiting_approval",
        request_id: "req-7",
        approval: { tool: "write_file", reason: "Validation requise." },
      }),
      frame("agent.resuming", { request_id: "req-7", task_id: "t3", role: "data_extractor" }),
      frame("agent.worker.start", { task_id: "t3", role: "data_extractor" }),
      frame("agent.worker.result", { task_id: "t3", role: "data_extractor", summary: "OK" }),
      frame("agent.synthesizing", { worker_errors: 0 }),
      frame("agent.done", { status: "completed", final_answer: "Terminé." }),
    ];
    const timeline: FlowEvent[] = stored
      .map((f) => sseToFlowEvent(f, 0, noSub))
      .filter((e): e is FlowEvent => e !== null);
    const g = reduceTimeline(timeline);
    expect(timeline.some((e) => e.t === "resuming")).toBe(true);
    expect(g.runStatus).toBe("completed");
    expect(g.nodes["role:data_extractor"]!.status).toBe("ok");
  });
});

describe("reduceTimeline — déterminisme (régression « Maximum update depth »)", () => {
  it("rejoue deux fois la même timeline avec des arcs strictement identiques", () => {
    const events = demoTimeline();
    const a = reduceTimeline(events);
    const b = reduceTimeline(events);
    // Jadis, un compteur module-global (nextSeq) faisait changer les ids des
    // arcs d'outil à chaque appel : edgeOrder/edgeSig variaient à chaque rendu
    // du Replay/Heatmap, et le useEffect [edgeSig] de FlowMapPage appelait
    // setPulses en boucle → « Maximum update depth exceeded ».
    expect(b.edgeOrder).toEqual(a.edgeOrder);
    expect(Object.keys(b.edges)).toEqual(Object.keys(a.edges));
    for (const id of a.edgeOrder) {
      expect(b.edges[id]).toBeDefined();
    }
    // Le séquenceur est réinstancié par graphe, pas cumulé entre appels.
    expect(a.edgeSeq).toBe(b.edgeSeq);
  });

  it("un graphe vivant (live) attribue des ids uniques à chaque appel d'outil", () => {
    const g = initGraph();
    const evs: FlowEvent[] = [
      { t: "worker.start", at: 0, task_id: "t1", role: "web_search" },
      { t: "tool.start", at: 1, task_id: "t1", role: "web_search", tool: "web_search" },
      { t: "tool.result", at: 2, task_id: "t1", role: "web_search", tool: "web_search", status: "ok" },
      { t: "worker.start", at: 3, task_id: "t2", role: "web_search" },
      { t: "tool.start", at: 4, task_id: "t2", role: "web_search", tool: "web_search" },
    ];
    for (const e of evs) applyEvent(g, e);
    const toolIds = g.edgeOrder.filter((id) => id.startsWith("tool:"));
    // Deux appels du même outil par le même rôle → arcs distincts (start/résultat).
    expect(toolIds).toHaveLength(3);
    expect(new Set(toolIds).size).toBe(toolIds.length);
    expect(g.edgeSeq).toBe(3);
  });
});