/**
 * Tests du client métier migrés v1 : getModelSanity (rapport 200 sain,
 * 503 enveloppe v1 domaine + repli details.detail) et predict (URL, corps
 * model_name, normalisation 503), autres erreurs propagées.
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

  it("retombe sur error.details.detail quand error.message est vide", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          error: {
            code: "model_unhealthy",
            message: "",
            details: {
              status: "unhealthy",
              verdict: "fallback_base_model",
              detail: "Fallback base model détecté",
              min_confidence: 0.4,
              accuracy: 0.125,
              results: [],
            },
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
    fetchMock.mockResolvedValue(
      jsonResponse({ error: { code: "not_found", message: "introuvable" } }, 404)
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api" });
    const err = await expectApiError(client.getModelSanity("x"));

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
    expect(err.message).toBe("introuvable");
  });
});

describe("SentimentApiClient.predict (v1)", () => {
  it("interroge /api/v1/predict et passe la version dans le corps (model_name)", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        results: [
          { text: "a", sentiment: "positive", confidence: 0.9, model_version: "v1" },
        ],
        model_version: "v1",
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api", apiKey: "secret" });
    const res = await client.predict(["a"], "v1");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api/api/v1/predict");
    expect(init.method).toBe("POST");
    expect(init.headers["X-API-Key"]).toBe("secret");
    expect(init.body).toBe(JSON.stringify({ texts: ["a"], model_name: "v1" }));
    expect(res?.results?.[0]).toMatchObject({ sentiment: "positive", model_version: "v1" });
  });

  it("omet model_name quand aucune version n'est demandée", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ results: [] }));
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api" });
    await client.predict(["a"]);

    expect(fetchMock.mock.calls[0][1].body).toBe(JSON.stringify({ texts: ["a"] }));
  });

  it("normalise un 503 enveloppe v1 (modèle indisponible)", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          error: {
            code: "model_not_available",
            message: "Aucun modèle disponible",
            details: {},
          },
        },
        503
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api" });
    const err = await expectApiError(client.predict(["a"]));

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(503);
    expect(err.message).toBe("Aucun modèle disponible");
  });
});

describe("SentimentApiClient.reloadPredictor (v1)", () => {
  it("interroge /api/v1/predict/reload (POST, sans paramètre)", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ status: "reloaded", sanity: "ok" })
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api", apiKey: "k" });
    const res = await client.reloadPredictor("v1");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api/api/v1/predict/reload");
    expect(init.method).toBe("POST");
    expect(res).toEqual({ status: "reloaded", sanity: "ok" });
  });

  it("expose le message métier sur un rechargement refusé (503 model_unhealthy)", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          error: {
            code: "model_unhealthy",
            message: "Fallback base model détecté : précision 12% < 50%",
            details: { status: "reload_rejected", verdict: "fallback_base_model" },
          },
        },
        503
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api" });
    const err = await expectApiError(client.reloadPredictor());

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(503);
    expect(err.message).toContain("Fallback base model");
  });
});

describe("SentimentApiClient.train (v1)", () => {
  it("interroge /api/v1/train (POST) pour lancer un entraînement", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ job_id: "job-1", status: "pending" }, 202)
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api", apiKey: "k" });
    const job = await client.startTraining({ max_per_lang: 10 });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api/api/v1/train");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ max_per_lang: 10 });
    expect(init.headers).toMatchObject({ "X-API-Key": "k" });
    expect(job).toMatchObject({ job_id: "job-1", status: "pending" });
  });

  it("construit l'URL WebSocket /api/v1/train/stream avec le jeton en query", () => {
    const client = new SentimentApiClient({ baseUrl: "http://api", apiKey: "k" });

    expect(client.getTrainMetricsStreamUrl("job 1")).toBe(
      "ws://api/api/v1/train/stream/job%201?token=k"
    );
  });

  it("construit l'URL WebSocket sans query si aucune clé API", () => {
    const client = new SentimentApiClient({ baseUrl: "https://api.example.com" });

    expect(client.getTrainMetricsStreamUrl("job-1")).toBe(
      "wss://api.example.com/api/v1/train/stream/job-1"
    );
  });

  it("supprime une planification via /api/v1/train/schedules (DELETE, 204)", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api", apiKey: "k" });
    const res = await client.deleteSchedule("sched-1");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api/api/v1/train/schedules/sched-1");
    expect(init.method).toBe("DELETE");
    expect(res).toBeNull();
  });

  it("liste les jobs filtrés via /api/v1/train/jobs (query params)", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ total: 1, items: [], limit: 20, offset: 0 })
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api", apiKey: "k" });
    await client.listTrainingJobs({ status: "completed", limit: 20 });

    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://api/api/v1/train/jobs?status=completed&limit=20"
    );
  });
});

describe("SentimentApiClient.intentTrain (v1)", () => {
  it("interroge /api/v1/train/intent (POST) pour lancer un entraînement", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ job_id: "intent-1", status: "pending", kind: "intent" }, 202)
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api", apiKey: "k" });
    const job = await client.startIntentTraining({ epochs: 2 });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api/api/v1/train/intent");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ epochs: 2 });
    expect(job).toMatchObject({ job_id: "intent-1", kind: "intent" });
  });

  it("liste les jobs d'intention filtrés via /api/v1/train/intent/jobs", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ total: 0, items: [], limit: 50, offset: 0 })
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api", apiKey: "k" });
    await client.listIntentTrainingJobs({ status: "failed", limit: 50 });

    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://api/api/v1/train/intent/jobs?status=failed&limit=50"
    );
  });

  it("active une version via /api/v1/train/intent/activate (corps {version})", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ status: "activated", version: "20260905T120000Z" })
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api", apiKey: "k" });
    const res = await client.activateIntentVersion("20260905T120000Z");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api/api/v1/train/intent/activate");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ version: "20260905T120000Z" });
    expect(res).toEqual({ status: "activated", version: "20260905T120000Z" });
  });
});

describe("SentimentApiClient.modelsAndEvaluate (v1)", () => {
  it("interroge /api/v1/models/details pour le catalogue", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse([
        { name: "20260905", path: "/x/20260905", created_at: 1.0, active: true },
      ])
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api", apiKey: "k" });
    const models = await client.listModels();

    expect(fetchMock.mock.calls[0][0]).toBe("http://api/api/v1/models/details");
    expect(models).toHaveLength(1);
  });

  it("interroge /api/v1/models/active pour le pointeur", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ activated: false }));
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api", apiKey: "k" });
    await client.getActiveModel();

    expect(fetchMock.mock.calls[0][0]).toBe("http://api/api/v1/models/active");
  });

  it("active une version via POST /api/v1/models/{name}/activate", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ activated: true, version: "v1" }));
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api", apiKey: "k" });
    await client.activateModel("v1");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api/api/v1/models/v1/activate");
    expect(init.method).toBe("POST");
  });

  it("supprime une version via DELETE /api/v1/models/{name}", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ deleted: true, name: "bad", verdict: "model_unavailable" })
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api", apiKey: "k" });
    await client.deleteModel("bad");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api/api/v1/models/bad");
    expect(init.method).toBe("DELETE");
  });

  it("interroge /api/v1/evaluate/confusion avec query model+limit", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ n: 0, matrix: [] }));
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api", apiKey: "k" });
    await client.getConfusion({ model: "20260905", limit: 50 });

    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://api/api/v1/evaluate/confusion?model=20260905&limit=50"
    );
  });
});
describe("SentimentApiClient.authenticate (POST /api/v1/auth/token)", () => {
  it("envoie client_id/client_secret et retourne le jeton typé", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        token: "jwt.abc",
        token_type: "Bearer",
        expires_in: 900,
        role: "read",
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api" });
    const result = await client.authenticate("op@thinktuning.app", "s3cret", 900);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api/api/v1/auth/token");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(
      JSON.stringify({
        client_id: "op@thinktuning.app",
        client_secret: "s3cret",
        ttl_seconds: 900,
      })
    );
    expect(result).toMatchObject({ token: "jwt.abc", token_type: "Bearer", role: "read" });
  });

  it("omet ttl_seconds s'il n'est pas fourni", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ token: "t", token_type: "Bearer", expires_in: 900, role: "read" })
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new SentimentApiClient({ baseUrl: "http://api" });
    await client.authenticate("ci", "secret");

    expect(fetchMock.mock.calls[0][1].body).toBe(
      JSON.stringify({ client_id: "ci", client_secret: "secret" })
    );
  });
});