/**
 * Tests du transport HTTP (clientCore) : en-têtes, sérialisation JSON,
 * normalisation des erreurs, 204, _requestText, timeout.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, SentimentApiClientCore } from "./clientCore";

const fetchMock = vi.fn();

/** Attend une erreur d'un appel et la retape en ApiError. */
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

describe("SentimentApiClientCore._request", () => {
  it("envoie l'en-tête X-API-Key et sérialise le corps JSON", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClientCore({ baseUrl: "http://api", apiKey: "secret" });
    await client._request("/predict", { method: "POST", body: { texts: ["a"] } });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api/predict");
    expect(init.method).toBe("POST");
    expect(init.headers["X-API-Key"]).toBe("secret");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(init.body).toBe(JSON.stringify({ texts: ["a"] }));
  });

  it("construit la query string en ignorant les paramètres vides", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClientCore({ baseUrl: "http://api/" });
    await client._request("/train/jobs", { query: { status: "completed", model: undefined, limit: 5 } });

    expect(fetchMock.mock.calls[0][0]).toBe("http://api/train/jobs?status=completed&limit=5");
  });

  it("retourne null sur 204", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClientCore({ baseUrl: "http://api" });
    expect(await client._request("/annotate/merge", { method: "POST" })).toBeNull();
  });

  it("normalise une erreur HTTP avec le détail FastAPI", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "job_id introuvable" }, 404));
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClientCore({ baseUrl: "http://api" });
    const err = await expectApiError(client._request("/train/status/x"));

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
    expect(err.message).toBe("job_id introuvable");
  });

  it("convertit un échec réseau en ApiError (status 0, message explicite)", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClientCore({ baseUrl: "http://api" });
    const err = await expectApiError(client._request("/health"));

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(0);
    expect(err.message).toContain("Impossible de joindre l'API");
  });

  it("annule après le timeout (ApiError status 0)", async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal!.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError"))
          );
        })
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClientCore({ baseUrl: "http://api" });
    const pending = client._request("/health", { timeoutMs: 50 });
    vi.advanceTimersByTime(60);
    const err = await expectApiError(pending);

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(0);
    vi.useRealTimers();
  });
});

describe("SentimentApiClientCore._requestText", () => {
  it("renvoie le corps texte (métriques Prometheus)", async () => {
    fetchMock.mockResolvedValue(
      new Response("http_requests_total 42", {
        status: 200,
        headers: { "Content-Type": "text/plain" },
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClientCore({ baseUrl: "http://api" });
    const text = await client._requestText("/metrics");

    expect(text).toBe("http_requests_total 42");
  });

  it("lève une ApiError normalisée sur erreur HTTP", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "boom" }, 500));
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClientCore({ baseUrl: "http://api" });
    const err = await expectApiError(client._requestText("/metrics"));

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(500);
    expect(err.message).toBe("boom");
  });
});
