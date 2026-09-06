/**
 * Tests de normalisation getModelSanity (migration v1) : rapport 200 sain,
 * 503 enveloppe v1 domaine, 503 enveloppe legacy (compat déploiement mixte),
 * autres erreurs propagées.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, SentimentApiClient } from "./sentimentApiClient";

const fetchMock = vi.fn();

async function expectApiError(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (err) {
    return err as ApiError;
  }
  throw new Error("La promesse aurait dû rejeter.");
}

afterEach(() => {
  vi.restoreAllMocks();
  fetchMock.mockReset();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("SentimentApiClient.getModelSanity (v1)", () => {
  it("interroge /api/v1/health/model-sanity et retourne le rapport (200)", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        verdict: "ok",
        status: "ok",
        detail: "8/8 phrases correctement classées",
        min_confidence: 0.4,
        accuracy: 1.0,
        model: "v1",
        results: [],
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api" });
    const report = await client.getModelSanity("v1");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://api/api/v1/health/model-sanity?model_name=v1"
    );
    expect(report).toMatchObject({ verdict: "ok", httpStatus: 200 });
  });

  it("normalise un 503 enveloppe v1 en rapport affichable", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          error: {
            code: "model_unhealthy",
            message:
              "Modèle non entraîné détecté : confidence max 0.318 < seuil 0.40",
            details: {
              status: "unhealthy",
              verdict: "untrained",
              min_confidence: 0.4,
              accuracy: 0.25,
              model: "v2",
              results: [
                {
                  text: "Phrase négative",
                  lang: "fr",
                  expected: "negative",
                  predicted: "neutral",
                  confidence: 0.31,
                  correct: false,
                },
              ],
            },
          },
        },
        503
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api" });
    const report = (await client.getModelSanity("v2")) as Record<
      string,
      unknown
    >;

    expect(report.httpStatus).toBe(503);
    expect(report.verdict).toBe("untrained");
    expect(report.detail).toBe(
      "Modèle non entraîné détecté : confidence max 0.318 < seuil 0.40"
    );
    expect(report.accuracy).toBe(0.25);
    expect(Array.isArray(report.results)).toBe(true);
  });

  it("normalise encore un 503 enveloppe legacy (déploiement mixte)", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          detail: {
            status: "unhealthy",
            verdict: "fallback_base_model",
            detail: "Fallback base model détecté",
            min_confidence: 0.4,
            accuracy: 0.125,
            results: [],
          },
        },
        503
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api" });
    const report = (await client.getModelSanity()) as Record<string, unknown>;

    expect(report.httpStatus).toBe(503);
    expect(report.verdict).toBe("fallback_base_model");
    expect(report.detail).toBe("Fallback base model détecté");
  });

  it("propage les erreurs non-503 (réseau, 404, ...)", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "introuvable" }, 404));
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api" });
    const err = await expectApiError(client.getModelSanity("x"));

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
  });
});