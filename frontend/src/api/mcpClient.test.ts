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
  orchestrateViaMcpStream,
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

  it("ignore la sentinelle data: [DONE] qui clôt le flux (P0 SCRUM-151)", () => {
    const body = 'event: message\ndata: {"ok":true}\n\ndata: [DONE]\n\n';
    expect(parseSseData(body)).toBe('{"ok":true}');
  });

  it("arrête la lecture à la sentinelle [DONE] (données post-[DONE] ignorées)", () => {
    const body = 'data: {"a":1}\ndata: [DONE]\ndata: {"b":2}\n\n';
    expect(parseSseData(body)).toBe('{"a":1}');
  });

  it("n'accepte pas un flux réduit à la sentinelle [DONE]", () => {
    expect(() => parseSseData("data: [DONE]\n\n")).toThrow(McpTransportError);
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

  it("décode initialize/ping/tools/list quand le flux se termine par data: [DONE]", async () => {
    // P0 (SCRUM-151) : le serveur MCP termine TOUT flux par la sentinelle —
    // elle ne doit jamais casser le décodage JSON-RPC du transport.
    fetchMock.mockImplementation(
      () =>
        new Response(
          'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\ndata: [DONE]\n\n',
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new McpSseClient({ baseUrl: "http://api" });
    await expect(client.initialize()).resolves.toEqual({ tools: [] });
    await expect(client.ping()).resolves.toEqual({ tools: [] });
    await expect(client.listTools()).resolves.toEqual([]);
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
    expect(sent.params.arguments.mode).toBe("multi_agent");
  });

  it("transmet explicitement le mode mono-agent demandé par l'IHM", async () => {
    fetchMock.mockResolvedValue(sseResponse(JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      result: {
        content: [{ type: "text", text: JSON.stringify({ answer: "Réponse", status: "completed" }) }],
        isError: false,
      },
    })));
    vi.stubGlobal("fetch", fetchMock);

    await orchestrateViaMcp(
      { prompt: "Réponse simple", mode: "mono_agent" },
      { baseUrl: "http://api" },
    );

    const [, init] = fetchMock.mock.calls[0];
    const sent = JSON.parse(init.body as string);
    expect(sent.params.arguments.mode).toBe("mono_agent");
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

describe("orchestrateViaMcpStream", () => {
  it("transmet le mode multi-agent et relaie les événements workers MCP", async () => {
    const rpc = {
      jsonrpc: "2.0",
      id: 1,
      result: {
        content: [{
          type: "text",
          text: JSON.stringify({
            answer: "Réponse multi-agent",
            status: "success",
            plan: [{ task_id: "t1", role: "analyst", subtask: "Analyser" }],
            workers: [{ task_id: "t1", role: "analyst", status: "completed" }],
          }),
        }],
        isError: false,
      },
    };
    fetchMock.mockResolvedValue(new Response(
      [
        'event: orchestrate.worker',
        'data: {"event":"orchestrate.worker","worker_id":"t1","status":"completed"}',
        '',
        'event: orchestrate.done',
        `data: ${JSON.stringify(rpc)}`,
        '',
        'data: [DONE]',
        '',
      ].join("\n"),
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const events: Array<Record<string, unknown>> = [];
    await orchestrateViaMcpStream(
      {
        prompt: "Analyse",
        mode: "multi_agent",
        parallel: true,
        event_granularity: "summary",
      },
      (event) => events.push(event as Record<string, unknown>),
      { baseUrl: "http://api" },
    );

    const [, init] = fetchMock.mock.calls[0];
    const sent = JSON.parse(init.body as string);
    expect(sent.params.arguments).toMatchObject({
      mode: "multi_agent",
      parallel: true,
      event_granularity: "summary",
    });
    expect(events[0]).toEqual({
      multi_agent: {
        event: "orchestrate.worker",
        worker_id: "t1",
        status: "completed",
      },
    });
  });

  it("normalise la réflexion et le payload core_tool du flux MCP", async () => {
    const rpc = {
      jsonrpc: "2.0",
      id: 1,
      result: {
        content: [{
          type: "text",
          text: JSON.stringify({ answer: "Réponse", status: "completed" }),
        }],
        isError: false,
      },
    };
    fetchMock.mockResolvedValue(new Response(
      [
        'event: orchestrate.thinking',
        'data: {"thinking_delta":"Réflexion"}',
        '',
        'event: orchestrate.tool',
        'data: {"core_tool":{"event":"tool_start","tool":"now","args":{}}}',
        '',
        'event: orchestrate.done',
        `data: ${JSON.stringify(rpc)}`,
        '',
        'data: [DONE]',
        '',
      ].join("\n"),
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const events: Array<Record<string, unknown>> = [];
    const result = await orchestrateViaMcpStream(
      { prompt: "Analyse", enable_thinking: true },
      (event) => events.push(event as Record<string, unknown>),
      { baseUrl: "http://api" },
    );

    const [, init] = fetchMock.mock.calls[0];
    const sent = JSON.parse(init.body as string);
    expect(sent.params.arguments.mode).toBe("multi_agent");
    expect(events[0]).toEqual({ thinking_delta: "Réflexion" });
    expect(events[1].tool).toEqual({
      event: "tool_start",
      tool: "now",
      args: {},
    });
    expect(result.answer).toBe("Réponse");
  });

  it("attend la réponse JSON-RPC finale sous event: message après started/heartbeats", async () => {
    const rpc = {
      jsonrpc: "2.0",
      id: 1,
      result: {
        content: [{
          type: "text",
          text: JSON.stringify({ answer: "Réponse finale", status: "completed" }),
        }],
        isError: false,
      },
    };
    fetchMock.mockResolvedValue(new Response(
      [
        "event: orchestrate.started",
        'data: {"status":"started"}',
        "",
        ": heartbeat",
        "",
        "event: message",
        `data: ${JSON.stringify(rpc)}`,
        "",
        "data: [DONE]",
        "",
      ].join("\n"),
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const events: Array<Record<string, unknown>> = [];
    const result = await orchestrateViaMcpStream(
      { prompt: "info cpu et gpu ?", mode: "multi_agent" },
      (event) => events.push(event as Record<string, unknown>),
      { baseUrl: "http://api" },
    );

    expect(events).toEqual([{ multi_agent: { status: "started" } }, { rpc }]);
    expect(result.answer).toBe("Réponse finale");
  });

  it("relaie les événements legacy du planner, des workers et de la synthèse", async () => {
    const rpc = {
      jsonrpc: "2.0",
      id: 1,
      result: {
        content: [{
          type: "text",
          text: JSON.stringify({ answer: "Réponse finale", status: "completed" }),
        }],
        isError: false,
      },
    };
    fetchMock.mockResolvedValue(new Response(
      [
        'event: agent.plan',
        'data: {"event":"agent.plan","plan":[{"task_id":"t1"}]}',
        "",
        'event: agent.worker.thinking',
        'data: {"event":"agent.worker.thinking","task_id":"t1","thinking":"Je vérifie."}',
        "",
        'event: agent.worker.result',
        'data: {"event":"agent.worker.result","task_id":"t1","status":"ok"}',
        "",
        'event: agent.synthesizing',
        'data: {"event":"agent.synthesizing","status":"running"}',
        "",
        'event: agent.phase',
        'data: {"event":"agent.phase","phase":"synthesis","status":"timeout","reason":"synthesis_timeout"}',
        "",
        "event: message",
        `data: ${JSON.stringify(rpc)}`,
        "",
        "data: [DONE]",
        "",
      ].join("\n"),
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const events: Array<Record<string, unknown>> = [];
    const result = await orchestrateViaMcpStream(
      { prompt: "info cpu et gpu ?", enable_thinking: true },
      (event) => events.push(event as Record<string, unknown>),
      { baseUrl: "http://api" },
    );

    expect(events).toEqual([
      { multi_agent: { event: "agent.plan", plan: [{ task_id: "t1" }] } },
      {
        multi_agent: {
          event: "agent.worker.thinking",
          task_id: "t1",
          thinking: "Je vérifie.",
        },
      },
      {
        multi_agent: {
          event: "agent.worker.result",
          task_id: "t1",
          status: "ok",
        },
      },
      { multi_agent: { event: "agent.synthesizing", status: "running" } },
      {
        multi_agent: {
          event: "agent.phase",
          phase: "synthesis",
          status: "timeout",
          reason: "synthesis_timeout",
        },
      },
      {
        phase: {
          event: "agent.phase",
          phase: "synthesis",
          status: "timeout",
          reason: "synthesis_timeout",
        },
      },
      { rpc },
    ]);
    expect(result.answer).toBe("Réponse finale");
  });

  it("utilise la réponse de agent.done si le serveur ferme sans message JSON-RPC", async () => {
    fetchMock.mockResolvedValue(new Response(
      [
        'event: agent.done',
        'data: {"event":"agent.done","status":"completed","answer":"Réponse de secours"}',
        "",
        "data: [DONE]",
        "",
      ].join("\n"),
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const result = await orchestrateViaMcpStream(
      { prompt: "info cpu et gpu ?", mode: "multi_agent" },
      () => undefined,
      { baseUrl: "http://api" },
    );

    expect(result).toMatchObject({
      answer: "Réponse de secours",
      status: "completed",
    });
  });

  it("conserve la réponse de agent.done si le JSON-RPC terminal est vide", async () => {
    const rpc = {
      jsonrpc: "2.0",
      id: 1,
      result: {
        content: [{ type: "text", text: JSON.stringify({ status: "completed" }) }],
        isError: false,
      },
    };
    fetchMock.mockResolvedValue(new Response(
      [
        'event: agent.done',
        'data: {"event":"agent.done","status":"completed","final_answer":"Réponse worker"}',
        "",
        "event: message",
        `data: ${JSON.stringify(rpc)}`,
        "",
        "data: [DONE]",
        "",
      ].join("\n"),
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const result = await orchestrateViaMcpStream(
      { prompt: "info cpu et gpu ?", mode: "multi_agent" },
      () => undefined,
      { baseUrl: "http://api" },
    );

    expect(result.answer).toBe("Réponse worker");
  });

  it("signale explicitement une orchestration terminée sans réponse", async () => {
    const rpc = {
      jsonrpc: "2.0",
      id: 1,
      result: {
        content: [{ type: "text", text: JSON.stringify({ status: "completed" }) }],
        isError: false,
      },
    };
    fetchMock.mockResolvedValue(new Response(
      [
        "event: message",
        `data: ${JSON.stringify(rpc)}`,
        "",
        "data: [DONE]",
        "",
      ].join("\n"),
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      orchestrateViaMcpStream(
        { prompt: "info cpu et gpu ?", mode: "multi_agent" },
        () => undefined,
        { baseUrl: "http://api" },
      ),
    ).rejects.toThrow("sans réponse finale");
  });

  it("signale l'agent.error observé quand le flux se termine sans JSON-RPC (P0/P1)", async () => {
    fetchMock.mockResolvedValue(new Response(
      [
        'event: agent.worker.thinking',
        'data: {"event":"agent.worker.thinking","task_id":"task-1","role":"ops","thinking":" about","phase":"lead"}',
        "",
        'event: agent.error',
        'data: {"event":"agent.error","message":"LLM injoignable pendant la synthèse."}',
        "",
        "data: [DONE]",
        "",
      ].join("\n"),
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      orchestrateViaMcpStream(
        { prompt: "info cpu et gpu ?", mode: "multi_agent" },
        () => undefined,
        { baseUrl: "http://api" },
      ),
    ).rejects.toThrow(/agent\.error.*LLM injoignable pendant la synthèse/);
  });

  it("remonte le texte de orchestrate.error au lieu d'une bulle vide (P1)", async () => {
    fetchMock.mockResolvedValue(new Response(
      [
        'event: orchestrate.error',
        'data: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"orchestration_deadline_reached"}],"isError":true}}',
        "",
        'event: message',
        'data: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"orchestration_deadline_reached"}],"isError":true}}',
        "",
        "data: [DONE]",
        "",
      ].join("\n"),
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      orchestrateViaMcpStream(
        { prompt: "info cpu et gpu ?", mode: "multi_agent" },
        () => undefined,
        { baseUrl: "http://api" },
      ),
    ).rejects.toThrow(/L'agent MCP a échoué/);
  });

  it("transmet run_id / resume_request_id / task_id (reprise ciblée) au transport", async () => {
    // P0 (SCRUM-151) : les trois identifiants de reprise sont indépendants et
    // transmis tels quels au tool orchestrate.
    const rpc = {
      jsonrpc: "2.0",
      id: 1,
      result: {
        content: [{
          type: "text",
          text: JSON.stringify({ answer: "Run repris.", status: "completed" }),
        }],
        isError: false,
      },
    };
    fetchMock.mockResolvedValue(new Response(
      [
        "event: message",
        `data: ${JSON.stringify(rpc)}`,
        "",
        "data: [DONE]",
        "",
      ].join("\n"),
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    await orchestrateViaMcpStream(
      {
        prompt: "Reprends",
        mode: "multi_agent",
        run_id: "run-77",
        resume_request_id: "req-1",
        task_id: "t1",
      },
      () => undefined,
      { baseUrl: "http://api" },
    );

    const [, init] = fetchMock.mock.calls[0];
    const sent = JSON.parse(init.body as string);
    expect(sent.params.arguments).toMatchObject({
      run_id: "run-77",
      resume_request_id: "req-1",
      task_id: "t1",
    });
  });

  it("préserve awaiting_approval avec request_id (demande) ≠ run_id (run)", async () => {
    // La carte de validation s'appuie sur request_id ; la relance post-
    // approbation réutilise run_id — les deux doivent traverser intacts.
    const rpc = {
      jsonrpc: "2.0",
      id: 1,
      result: {
        content: [{
          type: "text",
          text: JSON.stringify({
            answer: "En attente de validation humaine.",
            status: "awaiting_approval",
            awaiting_approval: true,
            request_id: "req-1",
            run_id: "run-77",
            task_id: "t1",
            approval: { tool: "write_file", args: {}, reason: "validation humaine requise" },
          }),
        }],
        isError: false,
      },
    };
    fetchMock.mockResolvedValue(new Response(
      [
        "event: message",
        `data: ${JSON.stringify(rpc)}`,
        "",
        "data: [DONE]",
        "",
      ].join("\n"),
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const result = await orchestrateViaMcpStream(
      { prompt: "Écris le fichier", mode: "mono_agent" },
      () => undefined,
      { baseUrl: "http://api" },
    );
    expect(result.awaiting_approval).toBe(true);
    expect(result.request_id).toBe("req-1");
    expect(result.run_id).toBe("run-77");
    expect(result.run_id).not.toBe(result.request_id);
    expect(result.task_id).toBe("t1");
    expect(result.approval?.tool).toBe("write_file");
  });
});