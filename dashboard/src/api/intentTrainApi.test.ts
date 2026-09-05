/**
 * Tests de la façade intentTrainApi (SCRUM-95).
 *
 * La façade ne transporte rien : on vérifie la délégation vers le client
 * partagé (payloads, normalisation des défauts de pagination, chaînage
 * activation + rechargement) avec un client factice (vi.fn).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SentimentApiClient } from "./sentimentApiClient";
import {
  DEFAULT_INTENT_BASE_MODEL,
  DEFAULT_INTENT_DATASET,
  activateIntentVersion,
  cancelIntentTraining,
  getIntentModelVersions,
  getIntentTrainingStatus,
  listIntentTrainingJobs,
  startIntentTraining,
} from "./intentTrainApi";

// Arrange : client factice (même surface que SentimentApiClient pour /train/intent).
function makeClient() {
  return {
    startIntentTraining: vi
      .fn()
      .mockResolvedValue({ job_id: "job-1", status: "pending" }),
    getIntentTrainingStatus: vi
      .fn()
      .mockResolvedValue({ job_id: "job-1", status: "running" }),
    cancelIntentTraining: vi
      .fn()
      .mockResolvedValue({ job_id: "job-1", status: "cancelled" }),
    listIntentTrainingJobs: vi
      .fn()
      .mockResolvedValue({ total: 0, items: [], limit: 20, offset: 0 }),
    getIntentModelVersions: vi
      .fn()
      .mockResolvedValue({ total: 1, items: ["v1"], active: "v1" }),
    activateIntentVersion: vi
      .fn()
      .mockResolvedValue({ status: "activated", version: "v9" }),
    reloadClassifier: vi.fn().mockResolvedValue({ ok: true }),
  };
}

let client: ReturnType<typeof makeClient>;

beforeEach(() => {
  client = makeClient();
});

const asClient = () => client as unknown as SentimentApiClient;

describe("intentTrainApi", () => {
  it("startIntentTraining délègue le payload tel quel", async () => {
    // Arrange
    const payload = { dataset_path: "d.jsonl", epochs: 2 };
    // Act
    const job = await startIntentTraining(asClient(), payload);
    // Assert
    expect(client.startIntentTraining).toHaveBeenCalledTimes(1);
    expect(client.startIntentTraining).toHaveBeenCalledWith(payload);
    expect(job?.job_id).toBe("job-1");
  });

  it("getIntentTrainingStatus délègue l'identifiant de job", async () => {
    // Act
    const snap = await getIntentTrainingStatus(asClient(), "abc");
    // Assert
    expect(client.getIntentTrainingStatus).toHaveBeenCalledWith("abc");
    expect(snap?.status).toBe("running");
  });

  it("cancelIntentTraining délègue l'annulation", async () => {
    // Act
    const job = await cancelIntentTraining(asClient(), "abc");
    // Assert
    expect(client.cancelIntentTraining).toHaveBeenCalledWith("abc");
    expect(job?.status).toBe("cancelled");
  });

  it("listIntentTrainingJobs applique les défauts de pagination", async () => {
    // Act
    await listIntentTrainingJobs(asClient());
    // Assert
    expect(client.listIntentTrainingJobs).toHaveBeenCalledWith({
      status: undefined,
      limit: 20,
      offset: 0,
    });
  });

  it("listIntentTrainingJobs transmet le filtre de statut", async () => {
    // Act
    await listIntentTrainingJobs(asClient(), { status: "failed", limit: 5 });
    // Assert
    expect(client.listIntentTrainingJobs).toHaveBeenCalledWith({
      status: "failed",
      limit: 5,
      offset: 0,
    });
  });

  it("getIntentModelVersions renvoie les versions et le pointeur actif", async () => {
    // Act
    const snap = await getIntentModelVersions(asClient());
    // Assert
    expect(client.getIntentModelVersions).toHaveBeenCalledTimes(1);
    expect(snap?.items).toEqual(["v1"]);
    expect(snap?.active).toBe("v1");
  });

  it("activateIntentVersion active PUIS recharge le classifieur (chaînage)", async () => {
    // Act
    await activateIntentVersion(asClient(), "v9");
    // Assert
    expect(client.activateIntentVersion).toHaveBeenCalledWith("v9");
    expect(client.reloadClassifier).toHaveBeenCalledWith("intent");
    // Le rechargement doit intervenir après l'activation (store -> runtime).
    expect(client.activateIntentVersion.mock.invocationCallOrder[0]).toBeLessThan(
      client.reloadClassifier.mock.invocationCallOrder[0]
    );
  });

  it("expose les défauts backend (dataset + base model)", () => {
    expect(DEFAULT_INTENT_DATASET).toBe("data/intent_dataset.jsonl");
    expect(DEFAULT_INTENT_BASE_MODEL).toBe(
      "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    );
  });
});