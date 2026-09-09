/**
 * Tests du transport MCP-over-SSE (mcpClient.ts) : parse SSE, aller-retour
 * JSON-RPC, normalisation des erreurs (HTTP / JSON-RPC / timeout / payload),
 * et helper d'orchestration du dashboard (mode « MCP » du chat).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  McpSseClient,
  McpTransportError,
  orchestrateViaMcp,
  parseSseData,
} from "./mcpClient";

const fetchMock = vi.fn();

afterEach(() => {
  vi.restoreAllMocks();
  fetchMock.mockReset();
});

/** Attend le rejet d'une promesse et le retape en McpTransportError. */
async function captureError(promise: Promise<unknown>): Promise<McpTransportError> {
  try {
    await promise;
  } catch (err) {
    return err as McpTransportError;
  }
  throw new Error("La promesse aurait dû rejeter.");
}

/** Réponse HTTP au format SSE à événement unique utilisée par POST /mcp/sse. */
function sseResponse(data: string, status = 200): Response {
  return new Response(`event: message\ndata: ${data}\n\n`, {
    status,
    headers: { "Content-Type": "text/event-stream" },
  });
}

describe("parseSseData", () => {
  it("extrait la charge utile data: d'un flux SSE simple", () => {
    expect(parseSseData('event: message\ndata: {"ok":true}\n\n')).toBe(
      '{"ok":true}'
    );
  });

  it("concatène plusieurs lignes data: avec « \\n » (gros payload JSON)", () => {
    const body = "data: {\"a\": 1,\ndata: \"b\"}\n\n";
    expect(parseSseData(body)).toBe('{"a": 1,\n"b"}');
  });

  it("ignore les lignes de garde et les champs event:/id:", () => {
    const body = ": ping\nid: 1\ndata: ok\n\n";
    expect(parseSseData(body)).toBe("ok");
  });

  it("lève McpTransportError sur un flux sans data:", () => {
    expect(() => parseSseData(": ping\n\n")).toThrow(McpTransportError);
  });
});

describe("McpSseClient.call", () => {
  it("POSTe un message JSON-RPC puis retourne result depuis le flux SSE", async () => {
    fetchMock.mockResolvedValue(sseResponse(JSON.stringify({ jsonrpc: "2.0", id: 1, result: { ok: true } })));
    vi.stubGlobal("fetch", fetchMock);

    const client = new McpSseClient({ baseUrl: "http://api", apiKey: "secret" });
    const result = await client.call<{ ok: boolean }>("ping");

    expect(result).toEqual({ ok: true });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api/mcp/sse");
    expect(init.method).toBe("POST");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(init.headers["Accept"]).toBe("text/event-stream");
    expect(init.headers["X-API-Key"]).toBe("secret");
    expect(init.headers["X-Client-Id"]).toBe("thinktuning-dashboard");
    expect(init.headers["Mcp-Session-Id"]).toBeTruthy();
    const sent = JSON.parse(init.body as string);
    expect(sent.jsonrpc).toBe("2.0");
    expect(sent.method).toBe("ping");
  });

  it("lève McpTransportError avec le code JSON-RPC sur error", async () => {
    fetchMock.mockResolvedValue(
      sseResponse(
        JSON.stringify({
          jsonrpc: "2.0",
          id: 1,
          error: { code: -32601, message: "Method not found" },
        })
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new McpSseClient({ baseUrl: "http://api" });
    const err = await captureError(client.call("tools/call", { name: "ghost" }));
    expect(err).toBeInstanceOf(McpTransportError);
    expect(err.status).toBe(200);
    expect(err.code).toBe(-32601);
    expect(err.message).toContain("Method not found");
  });

  it("normalise une erreur HTTP (enveloppe v1 detail)", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "MCP_FIRST: endpoint lisible uniquement" }), {
        status: 403,
        headers: { "Content-Type": "application/json" },
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new McpSseClient({ baseUrl: "http://api" });
    const err = await captureError(client.call("tools/call"));
    expect(err).toBeInstanceOf(McpTransportError);
    expect(err.status).toBe(403);
    expect(err.message).toContain("MCP_FIRST");
  });

  it("lève une erreur de timeout (signal aborté, statut 0)", async () => {
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

    const client = new McpSseClient({ baseUrl: "http://api", timeoutMs: 50 });
    const pending = client.call("ping");
    vi.advanceTimersByTime(60);
    const err = await captureError(pending);
    expect(err).toBeInstanceOf(McpTransportError);
    expect(err.status).toBe(0);
    expect(err.message).toContain("dépassé le délai");
    vi.useRealTimers();
  });
});

describe("orchestrateViaMcp", () => {
  it("décode le JSON du content.text d'un tools/call orchestrate", async () => {
    const orchestrateText = JSON.stringify({
      answer: "Résultat de l'orchestration",
      thinking: "Analyse en cours.",
      status: "completed",
      actions: [{ tool: "read_file", status: "completed" }],
      rounds_used: 2,
      tool_calls_used: 1,
    });
    fetchMock.mockResolvedValue(
      sseResponse(
        JSON.stringify({
          jsonrpc: "2.0",
          id: 1,
          result: {
            content: [{ type: "text", text: orchestrateText }],
            isError: false,
          },
        })
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await orchestrateViaMcp(
      { prompt: "Analyse ce répertoire" },
      { baseUrl: "http://api" }
    );
    expect(result.answer).toBe("Résultat de l'orchestration");
    expect(result.thinking).toBe("Analyse en cours.");
    expect(result.status).toBe("completed");
    expect(result.actions?.[0]?.tool).toBe("read_file");

    // Le argumentaire part bien avec prompt requis.
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api/mcp/sse");
    const sent = JSON.parse(init.body as string);
    expect(sent.method).toBe("tools/call");
    expect(sent.params.name).toBe("orchestrate");
    expect(sent.params.arguments.prompt).toBe("Analyse ce répertoire");
  });

  it("transmet le réglage du mode Réflexion au tool orchestrate", async () => {
    fetchMock.mockResolvedValue(
      sseResponse(
        JSON.stringify({
          jsonrpc: "2.0",
          id: 1,
          result: {
            content: [{
              type: "text",
              text: JSON.stringify({
                answer: "Réponse",
                thinking: "Trace",
                status: "completed",
              }),
            }],
            isError: false,
          },
        })
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    await orchestrateViaMcp(
      { prompt: "Réfléchis", enable_thinking: true },
      { baseUrl: "http://api" }
    );

    const [, init] = fetchMock.mock.calls[0];
    const sent = JSON.parse(init.body as string);
    expect(sent.params.arguments.enable_thinking).toBe(true);
  });

  it("lève une erreur lisible quand le tool répond isError: true", async () => {
    fetchMock.mockResolvedValue(
      sseResponse(
        JSON.stringify({
          jsonrpc: "2.0",
          id: 1,
          result: {
            content: [{ type: "text", text: "Argument(s) requis manquant(s) : prompt" }],
            isError: true,
          },
        })
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    const err = await captureError(
      orchestrateViaMcp({ prompt: "" }, { baseUrl: "http://api" })
    );
    expect(err).toBeInstanceOf(McpTransportError);
    expect(err.message).toContain("L'agent MCP a échoué");
  });
});