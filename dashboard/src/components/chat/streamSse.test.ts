/**
 * Tests du parser SSE : blocs par ligne vide, data multiligne, CRLF,
 * champ event:, sentinelle [DONE], flux tronqué.
 */
import { describe, expect, it } from "vitest";
import { readNamedSseEvents, readSseEvents } from "./streamSse";

/** Construit un ReadableStream à partir d'un texte encodé UTF-8. */
function streamOf(text: string): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(text));
      controller.close();
    },
  });
}

async function collect<T>(gen: AsyncGenerator<T, void, unknown>): Promise<T[]> {
  const out: T[] = [];
  for await (const item of gen) out.push(item);
  return out;
}

describe("readSseEvents", () => {
  it("émet un événement par bloc data:", async () => {
    const events = await collect(
      readSseEvents(streamOf('data: {"delta":"Bon"}\n\ndata: {"delta":"jour"}\n\n'))
    );
    expect(events).toEqual(['{"delta":"Bon"}', '{"delta":"jour"}']);
  });

  it("concatène les lignes data: d'un même bloc avec \\n", async () => {
    const events = await collect(
      readSseEvents(streamOf("data: ligne1\ndata: ligne2\n\n"))
    );
    expect(events).toEqual(["ligne1\nligne2"]);
  });

  it("n'émet PAS un événement pour des data: sans ligne vide séparatrice", async () => {
    // Un seul bloc de 2 data: -> un seul événement (spec SSE).
    const events = await collect(readSseEvents(streamOf("data: a\ndata: b\n")));
    expect(events).toEqual(["a\nb"]);
  });

  it("gère le CRLF et les commentaires", async () => {
    const events = await collect(
      readSseEvents(streamOf(": ping\r\ndata: ok\r\n\r\n"))
    );
    expect(events).toEqual(["ok"]);
  });

  it("préserve la sentinelle [DONE]", async () => {
    const events = await collect(readSseEvents(streamOf("data: [DONE]\n\n")));
    expect(events).toEqual(["[DONE]"]);
  });

  it("retire une seule espace de tête, sans trim() destructif", async () => {
    const events = await collect(readSseEvents(streamOf("data:  deux espaces \n\n")));
    expect(events).toEqual([" deux espaces "]);
  });
});

describe("readNamedSseEvents", () => {
  it("associe le champ event: au bloc courant puis retombe sur message", async () => {
    const events = await collect(
      readNamedSseEvents(
        streamOf("event: agent.plan\ndata: {\"a\":1}\n\ndata: {\"b\":2}\n\n")
      )
    );
    expect(events).toEqual([
      { event: "agent.plan", data: '{"a":1}' },
      { event: "message", data: '{"b":2}' },
    ]);
  });

  it("émet le dernier bloc même si le flux se termine sans ligne vide", async () => {
    const events = await collect(
      readNamedSseEvents(streamOf("event: agent.done\ndata: [DONE]\n"))
    );
    expect(events).toEqual([{ event: "agent.done", data: "[DONE]" }]);
  });

  it("traite les fragments multi-chunks (paquets réseau arbitraires)", async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode("event: agent.pl"));
        controller.enqueue(encoder.encode("an\ndata: {\"x\":1}\n\ndata"));
        controller.enqueue(encoder.encode(": {\"y\":2}\n\n"));
        controller.close();
      },
    });
    const events = await collect(readNamedSseEvents(body));
    expect(events).toEqual([
      { event: "agent.plan", data: '{"x":1}' },
      { event: "message", data: '{"y":2}' },
    ]);
  });
});
