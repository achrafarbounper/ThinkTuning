/**
 * flowApi.test.ts — Authentification des appels de la page Flow Map.
 *
 * Les appels bruts de flowApi doivent suivre la même hiérarchie que le reste
 * du dashboard (clientCore._headers / ChatWindow.resolveAuthHeaders) : session
 * JWT (thinktuning.authSession) PRIORITAIRE → Authorization: Bearer ; sinon
 * repli X-API-Key. Vérifié sur le flux POST et la liste GET.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SESSION_KEY } from "./authSession";
import { listFlowSessions, streamMultiFlow } from "./flowApi";

/** Réponse SSE vide (flux fermé immédiatement) : la lecture d'événements se termine. */
function emptySseResponse(): Response {
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } }
  );
}

const fetchMock = vi.fn();

function seedJwtSession(): void {
  window.localStorage.setItem(
    SESSION_KEY,
    JSON.stringify({
      clientId: "achraf.arboun.per@gmail.com",
      token: "jwt.abc",
      tokenType: "Bearer",
      role: "read",
      issuedAt: Date.now(),
      expiresIn: 900,
    })
  );
}

function seedApiConfig(apiKey?: string): void {
  window.localStorage.setItem(
    "thinktuning.apiConfig",
    JSON.stringify({ baseUrl: "http://api", apiKey })
  );
}

describe("flowApi — authentification", () => {
  beforeEach(() => {
    window.localStorage.clear();
    fetchMock.mockReset();
    fetchMock.mockResolvedValue(emptySseResponse());
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("streamMultiFlow envoie Authorization: Bearer avec une session JWT valide", async () => {
    seedJwtSession();

    await streamMultiFlow({ prompt: "Analyse le sujet", onEvent: () => {} });

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/agent/multi/ask/stream");
    expect(init.headers).toMatchObject({ Authorization: "Bearer jwt.abc" });
    expect(init.headers["X-API-Key"]).toBeUndefined();
  });

  it("streamMultiFlow replie sur X-API-Key sans session JWT", async () => {
    seedApiConfig("clef-api"); // config legacy persistée contenant la clé

    await streamMultiFlow({ prompt: "Analyse le sujet", onEvent: () => {} });

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/agent/multi/ask/stream");
    expect(init.headers).toMatchObject({ "X-API-Key": "clef-api" });
    expect(init.headers.Authorization).toBeUndefined();
  });

  it("listFlowSessions (GET /agent/flow) porte le Bearer JWT", async () => {
    seedJwtSession();
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ flows: [], statuses: [] }), { status: 200 })
    );

    await listFlowSessions();

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/agent/flow");
    expect(init.headers).toMatchObject({ Authorization: "Bearer jwt.abc" });
  });
});