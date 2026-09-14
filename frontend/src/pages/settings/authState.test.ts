/**
 * Tests de la résolution d'état d'authentification (pages/settings/authState).
 * Le contrat clé : la session JWT est prioritaire sur la clé API — même règle
 * que le transport (api/clientCore attache le Bearer avant X-API-Key).
 */
import { describe, expect, it } from "vitest";
import { authStateView, resolveAuthState } from "./authState";

describe("authState — résolution de l'état effectif", () => {
  it("priorise la session JWT sur la clé API (même règle que le transport)", () => {
    expect(resolveAuthState("jwt.abc", "secret")).toBe("jwt");
  });

  it("retourne api-key sans session JWT", () => {
    expect(resolveAuthState("", "secret")).toBe("api-key");
    expect(resolveAuthState(null, "secret")).toBe("api-key");
    expect(resolveAuthState(undefined, "secret")).toBe("api-key");
  });

  it("retourne none sans aucun identifiant", () => {
    expect(resolveAuthState("", "")).toBe("none");
    expect(resolveAuthState(null, null)).toBe("none");
    expect(resolveAuthState(undefined, undefined)).toBe("none");
  });

  it("ignore les valeurs blanches (espaces uniquement)", () => {
    expect(resolveAuthState("   ", "secret")).toBe("api-key");
    expect(resolveAuthState("jwt.abc", "   ")).toBe("jwt");
    expect(resolveAuthState("   ", "   ")).toBe("none");
  });
});

describe("authState — rendu du badge", () => {
  it("fournit un libellé, une couleur et une aide pour chaque état", () => {
    for (const state of ["jwt", "api-key", "none"] as const) {
      const view = authStateView(state);
      expect(view.label.length).toBeGreaterThan(0);
      expect(view.tone).toMatch(/^(completed|running|pending|failed|cancelled)$/);
      expect(view.hint.length).toBeGreaterThan(0);
    }
  });

  it("distingue visuellement une session persistante d'une clé mémoire", () => {
    expect(authStateView("jwt").tone).toBe("completed");
    expect(authStateView("api-key").tone).toBe("pending");
    expect(authStateView("none").tone).toBe("failed");
  });

  it("explique la non-persistance de la clé (P1 SEC) dans l'aide", () => {
    expect(authStateView("api-key").hint).toContain("rafraîchissement");
    expect(authStateView("jwt").hint).toContain("survit au rafraîchissement");
  });
});
