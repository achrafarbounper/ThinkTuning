/**
 * Tests du module de session d'authentification (api/authSession).
 */
import { afterEach, describe, expect, it } from "vitest";
import { ApiError } from "./clientCore";
import {
  AUTH_TTL_SECONDS,
  SESSION_KEY,
  buildAuthSession,
  isSessionValid,
  mapAuthErrorMessage,
  readStoredSession,
  sessionExpiresAt,
} from "./authSession";

afterEach(() => {
  window.localStorage.clear();
});

describe("authSession — construction et validité", () => {
  it("construit une session valide avec le TTL demandé et gère l'expiration", () => {
    const session = buildAuthSession("op", "jwt.abc", {
      role: "admin",
      expiresIn: 3600,
      issuedAt: 1_000,
    });

    expect(session.clientId).toBe("op");
    expect(session.token).toBe("jwt.abc");
    expect(session.tokenType).toBe("Bearer");
    expect(session.role).toBe("admin");
    expect(sessionExpiresAt(session)).toBe(1_000 + 3600 * 1000);
    expect(isSessionValid(session, 1_000 + 3599 * 1000)).toBe(true);
    expect(isSessionValid(session, 1_000 + 3601 * 1000)).toBe(false);
  });

  it("rejette les sessions nulles ou sans jeton", () => {
    expect(isSessionValid(null)).toBe(false);
    expect(isSessionValid(undefined)).toBe(false);
    expect(
      isSessionValid({
        clientId: "x",
        token: "",
        tokenType: "Bearer",
        role: "read",
        issuedAt: 0,
        expiresIn: 3600,
      })
    ).toBe(false);
  });

  it("persiste et relit une session depuis localStorage", () => {
    const session = buildAuthSession("op", "jwt.abc", { expiresIn: AUTH_TTL_SECONDS });
    window.localStorage.setItem(SESSION_KEY, JSON.stringify(session));

    const loaded = readStoredSession();
    expect(loaded).not.toBeNull();
    expect(loaded?.token).toBe("jwt.abc");
    expect(loaded?.clientId).toBe("op");
    expect(loaded?.expiresIn).toBe(AUTH_TTL_SECONDS);
  });

  it("renvoie null si le stockage est vide ou corrompu", () => {
    expect(readStoredSession()).toBeNull();
    window.localStorage.setItem(SESSION_KEY, "{pas du json");
    expect(readStoredSession()).toBeNull();
  });
});

describe("authSession — messages d'erreur", () => {
  it("traduit un 401 en « Identifiants invalides » (anti-énumération)", () => {
    expect(mapAuthErrorMessage(new ApiError("client_id ou secret invalide", 401))).toBe(
      "Identifiants invalides"
    );
  });

  it("traduit un échec réseau en message actionnable", () => {
    expect(mapAuthErrorMessage(new ApiError("network", 0))).toBe(
      "API injoignable : vérifiez le serveur"
    );
  });

  it("remonte le message d'un ApiError non-401", () => {
    expect(mapAuthErrorMessage(new ApiError("boom", 503))).toBe("boom");
  });

  it("gère les erreurs non-ApiError", () => {
    expect(mapAuthErrorMessage(new Error("autre échec"))).toBe("autre échec");
    expect(mapAuthErrorMessage("brut")).toBe("Connexion impossible");
  });
});