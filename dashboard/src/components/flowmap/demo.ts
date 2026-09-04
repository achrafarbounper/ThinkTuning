/**
 * demo.ts — Timeline de démonstration du « Agent Flow Map ».
 *
 * `demoTimeline()` construit une session multi-agents réaliste « à la
 * LangSmith » : planification, 4 workers spécialisés, appels d'outils
 * (recherche / code / transformation), une erreur de policy et une synthèse.
 * Les horodatages (ms relatifs à 0) servent directement le mode Replay.
 */

import type { FlowEvent } from "./types";

/** Construit une orchestration réaliste, timestamps en ms relatifs. */
export function demoTimeline(): FlowEvent[] {
  const events: FlowEvent[] = [];
  let at = 0;
  const tick = (ms: number) => {
    at += ms;
    return at;
  };

  events.push({
    t: "plan",
    at: tick(80),
    plan: [
      { task_id: "t1", role: "web_search", subtask: "Collecter des sources FR sur la dérive climatique" },
      { task_id: "t2", role: "code_analysis", subtask: "Analyser le script d'extraction fourni" },
      { task_id: "t3", role: "summarizer", subtask: "Synthétiser les arguments pour/contre" },
      { task_id: "t4", role: "report_writer", subtask: "Rédiger la synthèse finale" },
    ],
  });

  // Worker 1 — recherche web
  events.push({ t: "worker.start", at: tick(40), task_id: "t1", role: "web_search" });
  events.push({ t: "tool.start", at: tick(30), task_id: "t1", role: "web_search", tool: "web_search", args: '{"query":"dérive climatique sources françaises"}' });
  events.push({ t: "tool.result", at: tick(120), task_id: "t1", role: "web_search", tool: "web_search", status: "ok", summary: "12 résultats pertinents.", duration_ms: 118 });
  events.push({ t: "tool.start", at: tick(20), task_id: "t1", role: "web_search", tool: "http_get", args: '{"url":"https://ex.fr/article"}' });
  events.push({ t: "tool.result", at: tick(90), task_id: "t1", role: "web_search", tool: "http_get", status: "ok", summary: "200 OK · 3,4 ko.", duration_ms: 86 });
  events.push({ t: "worker.result", at: tick(30), task_id: "t1", role: "web_search", summary: "Sources trouvées et nettoyées.", duration_ms: 340 });

  // Worker 2 — analyse de code
  events.push({ t: "worker.start", at: tick(40), task_id: "t2", role: "code_analysis" });
  events.push({ t: "tool.start", at: tick(20), task_id: "t2", role: "code_analysis", tool: "read_file", args: '{"path":"extract.py"}' });
  events.push({ t: "tool.result", at: tick(60), task_id: "t2", role: "code_analysis", tool: "read_file", status: "ok", summary: "342 lignes.", duration_ms: 55 });
  events.push({ t: "tool.start", at: tick(15), task_id: "t2", role: "code_analysis", tool: "run_python", args: '{"snippet":"ast.parse(...)"}' });
  events.push({ t: "tool.result", at: tick(70), task_id: "t2", role: "code_analysis", tool: "run_python", status: "ok", summary: "Syntaxe valide ; 1 fuite mémoire.", duration_ms: 66 });
  events.push({ t: "worker.result", at: tick(25), task_id: "t2", role: "code_analysis", summary: "Analyse statique effectuée.", duration_ms: 210 });

  // Worker 3 — transformation / synthèse
  events.push({ t: "worker.start", at: tick(40), task_id: "t3", role: "summarizer" });
  events.push({ t: "tool.start", at: tick(15), task_id: "t3", role: "summarizer", tool: "text_summarize", args: '{"text":"...", "max": 200}' });
  events.push({ t: "tool.result", at: tick(80), task_id: "t3", role: "summarizer", tool: "text_summarize", status: "ok", summary: "Résumé généré.", duration_ms: 74 });

  // Worker 4 — rédaction (écriture bloquée par la policy)
  events.push({ t: "worker.start", at: tick(40), task_id: "t4", role: "report_writer" });
  events.push({ t: "tool.start", at: tick(15), task_id: "t4", role: "report_writer", tool: "write_file", args: '{"path":"rapport.md"}' });
  events.push({ t: "tool.result", at: tick(60), task_id: "t4", role: "report_writer", tool: "write_file", status: "error", summary: "Permission refusée (écriture hors sandbox).", duration_ms: 58 });
  events.push({ t: "worker.error", at: tick(20), task_id: "t4", role: "report_writer", error_code: "tool_not_allowed", message: "Écriture bloquée par la policy." });
  events.push({ t: "worker.result", at: tick(15), task_id: "t4", role: "report_writer", summary: "Rédaction impossible sans droits.", duration_ms: 110 });

  events.push({ t: "synthesizing", at: tick(60), worker_errors: 1 });
  events.push({
    t: "done",
    at: tick(120),
    answer:
      "Synthèse : la dérive climatique est documentée par plusieurs sources convergentes. " +
      "L'analyse de code a révélé un point fragile ; la rédaction du rapport a été bloquée par la policy.",
    duration_ms: 780,
  });

  return events;
}