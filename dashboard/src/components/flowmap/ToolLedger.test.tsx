/**
 * ToolLedger.test.tsx — Contrat de rendu du « Journal des outils ».
 *
 * Verrouille : liste ordonnée (#n), statut, agent exécutant (chip),
 * métadonnées dépliables (input/output), sélection croisée (arc ↔ ligne),
 * repli de l'overlay et état vide.
 */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { ToolLedger } from "./ToolLedger";
import type { ToolCallRecord } from "./types";

const records: ToolCallRecord[] = [
  {
    seq: 1,
    role: "web_search",
    taskId: "t1",
    tool: "web_search",
    category: "search",
    color: "#38bdf8",
    status: "ok",
    args: '{"query":"climat"}',
    summary: "12 résultats pertinents.",
    durationMs: 118,
    startedAt: 150,
    endedAt: 268,
    startEdgeId: "tool:web_search:web_search:1",
    resultEdgeId: "tool:web_search:web_search:2",
  },
  {
    seq: 2,
    role: "code_analysis",
    taskId: "t2",
    tool: "run_python",
    category: "code",
    color: "#a78bfa",
    status: "running",
    args: '{"snippet":"ast.parse(...)"}',
    startedAt: 1200,
    startEdgeId: "tool:code_analysis:run_python:3",
  },
];

function renderLedger(overrides: Partial<Parameters<typeof ToolLedger>[0]> = {}) {
  const props = {
    records,
    selectedEdgeId: null as string | null,
    onSelectEdge: vi.fn(),
    onSelectNode: vi.fn(),
    ...overrides,
  };
  const utils = render(<ToolLedger {...props} />);
  return { ...utils, props };
}

describe("ToolLedger — liste ordonnée des outils exécutés", () => {
  it("affiche le compteur d'appels et une entrée par appel (ordre #n)", () => {
    const { container } = renderLedger();
    expect(screen.getByText("Journal des outils")).toBeInTheDocument();
    expect(container.querySelectorAll(".fledger__item")).toHaveLength(2);
    expect(screen.getByText("#1")).toBeInTheDocument();
    expect(screen.getByText("#2")).toBeInTheDocument();
  });

  it("montre l'outil, la durée et l'agent exécutant (relation journal → agent)", () => {
    const { container } = renderLedger();
    const tools = Array.from(container.querySelectorAll(".fledger__tool")).map((el) => el.textContent);
    expect(tools).toEqual(["web_search", "run_python"]);
    const agents = Array.from(container.querySelectorAll(".fledger__agent")).map((el) => el.textContent);
    expect(agents.some((t) => t?.includes("web_search"))).toBe(true);
    expect(agents.some((t) => t?.includes("code_analysis"))).toBe(true);
    // Durée du premier appel (clos) + appel en cours du second (« … »).
    expect(screen.getByText(/118 ms/)).toBeInTheDocument();
    expect(screen.getByText(/… · t\+1\.2 s/)).toBeInTheDocument();
  });

  it("sélectionne l'arc de l'appel au clic sur la ligne (résultat si clos)", () => {
    const { props, container } = renderLedger();
    const rows = container.querySelectorAll(".fledger__main");
    fireEvent.click(rows[0]);
    expect(props.onSelectEdge).toHaveBeenCalledWith("tool:web_search:web_search:2");
    fireEvent.click(rows[1]);
    expect(props.onSelectEdge).toHaveBeenCalledWith("tool:code_analysis:run_python:3");
  });

  it("sélectionne le nœud de l'agent au clic sur le chip agent", () => {
    const { props, container } = renderLedger();
    const chips = container.querySelectorAll(".fledger__agent");
    fireEvent.click(chips[1]);
    expect(props.onSelectNode).toHaveBeenCalledWith("role:code_analysis");
  });

  it("déplie les métadonnées de l'appel (statut, sous-tâche, input, output)", () => {
    const { container } = renderLedger();
    expect(screen.queryByText("Input")).not.toBeInTheDocument();
    fireEvent.click(container.querySelector(".fledger__more")!);
    expect(screen.getByText("Input")).toBeInTheDocument();
    expect(screen.getByText("Output")).toBeInTheDocument();
    expect(screen.getByText('{"query":"climat"}')).toBeInTheDocument();
    expect(screen.getByText("12 résultats pertinents.")).toBeInTheDocument();
  });

  it("surligne la ligne de l'arc sélectionné (sync canvas ↔ journal)", () => {
    const { container } = renderLedger({ selectedEdgeId: "tool:web_search:web_search:2" });
    expect(container.querySelectorAll(".fledger__item.is-selected")).toHaveLength(1);
  });

  it("se replie et se déplie via l'entête", () => {
    const { container } = renderLedger();
    const toggle = screen.getByRole("button", { name: /Journal des outils/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector(".fledger__list")).toBeNull();
    expect(container.querySelector(".fledger--collapsed")).not.toBeNull();
  });

  it("affiche un état vide quand aucun outil n'a été exécuté", () => {
    renderLedger({ records: [] });
    expect(screen.getByText("Aucun outil exécuté pour le moment.")).toBeInTheDocument();
  });
});
