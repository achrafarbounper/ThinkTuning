/**
 * FlowCanvas.test.tsx — Contrat de rendu du « FlowCanvas » (UX SCRUM-98).
 *
 * Verrouille les ajouts de lisibilité et de hiérarchie :
 *  - badges de densité (agents / outils / arcs) visibles dans la toile ;
 *  - flèches directionnelles présentes sur les arcs (sens du flux) ;
 *  - bouton plein écran disponible lorsque l'API Fullscreen existe, et
 *    basculement de l'état (agrandissement de la toile à la demande).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { FlowCanvas } from "./FlowCanvas";
import { initGraph, reduceTimeline } from "./buildGraph";
import { demoTimeline } from "./demo";
import { computeHeat } from "./heat";
import { buildToolLedger } from "./ledger";
import type { Pulse } from "./types";

/** Graphe de démonstration — la même timeline qui alimente la page. */
function demoGraph() {
  const g = initGraph();
  Object.assign(g, reduceTimeline(demoTimeline()));
  return { nodes: Object.values(g.nodes), edges: g.edgeOrder.map((id) => g.edges[id]) };
}

beforeEach(() => {
  // jsdom n'implémente pas l'API Fullscreen : on la simule pour tester le bouton.
  (document.documentElement as unknown as { requestFullscreen: unknown }).requestFullscreen = vi.fn(() =>
    Promise.resolve(),
  );
  (document as unknown as { exitFullscreen: unknown }).exitFullscreen = vi.fn(() => Promise.resolve());
});

afterEach(() => {
  vi.restoreAllMocks();
  delete (document.documentElement as unknown as { requestFullscreen?: unknown }).requestFullscreen;
  delete (document as unknown as { exitFullscreen?: unknown }).exitFullscreen;
});

describe("FlowCanvas — lisibilité & hiérarchie visuelle", () => {
  it("affiche les badges de densité (agents / outils / arcs)", () => {
    const { nodes, edges } = demoGraph();
    render(
      <FlowCanvas
        nodes={nodes}
        edges={edges}
        pulses={[]}
        onPulseDone={() => {}}
        selected={null}
        onSelect={() => {}}
        heat={null}
        focusRelated={false}
      />,
    );
    expect(screen.getByText(/agent(s)?/)).toBeInTheDocument();
    const tools = edges.filter((e) => e.kind === "tool").length;
    expect(screen.getByText(new RegExp(`${tools} outil(s)?`))).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`${edges.length} arc(s)?`))).toBeInTheDocument();
  });

  it("rend une flèche directionnelle sur chaque arc (sens du flux visible)", () => {
    const { nodes, edges } = demoGraph();
    const { container } = render(
      <FlowCanvas
        nodes={nodes}
        edges={edges}
        pulses={[]}
        onPulseDone={() => {}}
        selected={null}
        onSelect={() => {}}
        heat={null}
        focusRelated={false}
      />,
    );
    const arrows = container.querySelectorAll(".fedge__arrow");
    expect(arrows.length).toBeGreaterThan(0);
    // Une flèche par arc (aller ET retour) : le graphe est entièrement orienté.
    expect(arrows.length).toBe(edges.length);
  });

  it("distingue visuellement l'orchestrateur (classe fnode--planner)", () => {
    const { nodes, edges } = demoGraph();
    const { container } = render(
      <FlowCanvas
        nodes={nodes}
        edges={edges}
        pulses={[]}
        onPulseDone={() => {}}
        selected={null}
        onSelect={() => {}}
        heat={null}
        focusRelated={false}
      />,
    );
    expect(container.querySelector(".fnode--planner")).not.toBeNull();
  });

  it("bascule en plein écran via le bouton dédié (agrandissement de la toile)", () => {
    const { nodes, edges } = demoGraph();
    const { container } = render(
      <FlowCanvas
        nodes={nodes}
        edges={edges}
        pulses={[]}
        onPulseDone={() => {}}
        selected={null}
        onSelect={() => {}}
        heat={null}
        focusRelated={false}
      />,
    );
    const zoomGroup = container.querySelector(".fc__zoom");
    expect(zoomGroup).not.toBeNull();
    const toggle = within(zoomGroup as HTMLElement).getByTitle(/plein écran/i);
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-pressed")).toBe("true");
  });

  it("reste stable en mode heatmap (heat) avec impulsions actives", () => {
    const { nodes, edges } = demoGraph();
    const heat = computeHeat(nodes, edges);
    const pulses: Pulse[] = edges.length ? [{ id: "p1", edgeId: edges[0].id, color: "#22d3ee" }] : [];
    const { container } = render(
      <FlowCanvas
        nodes={nodes}
        edges={edges}
        pulses={pulses}
        onPulseDone={() => {}}
        selected={{ type: "edge", id: edges[0]?.id ?? "" }}
        onSelect={() => {}}
        heat={heat}
        focusRelated
      />,
    );
    // Le graphe heatmap reste rendu, nœuds teintés et arcs épais par usage.
    expect(container.querySelectorAll(".fnode").length).toBe(nodes.length);
    expect(container.querySelectorAll(".fedge").length).toBe(edges.length);
  });

  it("affiche le journal des outils exécutés dans la toile (liste ordonnée)", () => {
    const g = initGraph();
    Object.assign(g, reduceTimeline(demoTimeline()));
    const ledger = buildToolLedger(g);
    const { container } = render(
      <FlowCanvas
        nodes={Object.values(g.nodes)}
        edges={g.edgeOrder.map((id) => g.edges[id])}
        pulses={[]}
        onPulseDone={() => {}}
        selected={null}
        onSelect={() => {}}
        heat={null}
        focusRelated={false}
        ledger={ledger}
      />,
    );
    const panel = container.querySelector(".fledger");
    expect(panel).not.toBeNull();
    // Une ligne par appel exécuté, dans l'ordre, avec le compteur en entête.
    expect(container.querySelectorAll(".fledger__item").length).toBe(ledger.length);
    expect(panel?.textContent).toContain("Journal des outils");
  });
});