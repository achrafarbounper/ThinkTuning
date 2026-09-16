/**
 * Tests du badge d'état MCP (L3 SCRUM-154) : preflight automatique, tonalités
 * (unknown / checking / ok / error) et re-diagnostic au clic.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { McpStatusBadge } from "./McpStatusBadge";

const { preflightMcpMock } = vi.hoisted(() => ({ preflightMcpMock: vi.fn() }));

vi.mock("../../api/mcpClient", () => ({
  preflightMcp: preflightMcpMock,
  // Miroir du module réel : le badge convertit toute exception du preflight en
  // erreur actionnable (concaténation de l'action).
  makeMcpErrorActionable: (error: unknown): string =>
    error instanceof Error ? `${error.message} — Action : (mock)` : String(error),
}));

describe("McpStatusBadge", () => {
  beforeEach(() => {
    preflightMcpMock.mockReset();
  });

  it("tonalité 'unknown' sans autoCheck, puis diagnostic OK au clic", async () => {
    preflightMcpMock.mockResolvedValue({
      ok: true,
      protocolVersion: "2025-06-18",
      serverName: "thinktuning-mcp",
      toolCount: 3,
      tools: ["orchestrate"],
    });

    render(<McpStatusBadge baseUrl="http://api" apiKey="secret" />);
    // Jamais diagnostiqué : gris, aucun appel.
    expect(screen.getByText("MCP : non diagnostiqué")).toBeInTheDocument();
    expect(preflightMcpMock).not.toHaveBeenCalled();

    // Convention du projet : fireEvent + waitFor, SANS `act()` explicite
    // (l'environnement de test n'expose pas IS_REACT_ACT_ENVIRONMENT ; un act
    // englobant mettrait les mises à jour asynchrones en file sans les vider).
    fireEvent.click(screen.getByRole("button", { name: "Diagnostic MCP" }));
    await waitFor(() =>
      expect(screen.getByText("MCP : OK (3 tools)")).toBeInTheDocument(),
    );
    expect(preflightMcpMock).toHaveBeenCalledWith({ baseUrl: "http://api", apiKey: "secret" });
    // Tonalité verte (classe CSS).
    expect(document.querySelector(".mcp-status--ok")).not.toBeNull();
  });

  it("autoCheck lance le preflight au montage et expose l'erreur actionnable", async () => {
    preflightMcpMock.mockResolvedValue({
      ok: false,
      error: "Clé invalide — Action : vérifiez votre clé API dans les Paramètres.",
      status: 401,
    });

    render(<McpStatusBadge baseUrl="http://api" apiKey="wrong" autoCheck />);

    // Le diagnostic démarre seul (deferred d'un tick) et termine en erreur.
    await waitFor(() => expect(preflightMcpMock).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByText("MCP : indisponible")).toBeInTheDocument(),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/vérifiez votre clé API/);
    expect(document.querySelector(".mcp-status--error")).not.toBeNull();
  });

  it("convertit un rejet du preflight en erreur actionnable (aucun rejet non géré)", async () => {
    // Panne réseau : le client MCP rejette (fetch impossible). Le badge doit
    // rester actionnable plutôt que de propager une promesse rejetée.
    preflightMcpMock.mockRejectedValue(new Error("Failed to fetch"));

    render(<McpStatusBadge baseUrl="http://api" apiKey="secret" autoCheck />);

    await waitFor(() =>
      expect(screen.getByText("MCP : indisponible")).toBeInTheDocument(),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      /Failed to fetch — Action : \(mock\)/,
    );
    // Le diagnostic est terminé : plus aucune vérification en cours.
    expect(screen.queryByText("MCP : diagnostic…")).not.toBeInTheDocument();
  });

  it("expose les métadonnées du serveur en cas de succès", async () => {
    preflightMcpMock.mockResolvedValue({
      ok: true,
      protocolVersion: "2025-06-18",
      serverName: "thinktuning-mcp",
      toolCount: 1,
      tools: ["orchestrate"],
    });

    render(<McpStatusBadge baseUrl="http://api" apiKey="secret" autoCheck />);

    await waitFor(() =>
      expect(screen.getByText("MCP : OK (1 tools)")).toBeInTheDocument(),
    );
    // Info-bulle détaillée (protocole + serveur).
    expect(
      screen.getByTitle(/Protocole 2025-06-18 — serveur thinktuning-mcp/),
    ).toBeInTheDocument();
  });
});
