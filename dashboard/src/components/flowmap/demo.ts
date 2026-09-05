/**
 * demo.ts — Timeline de démonstration du « Agent Flow Map ».
 *
 * `demoTimeline()` construit une session multi-agents réaliste « à la
 * LangSmith » : planification, 4 workers spécialisés, appels d'outils, une
 * validation humaine (worker bloqué → awaiting_approval), la reprise native
 * (resuming → re-dispatch du même worker), une erreur de policy et une
 * synthèse. Les horodatages (ms relatifs à 0) servent directement le Replay.
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
      { task_id: "t3", role: "data_extractor", subtask: "Extraire et consolider les données chiffrées" },
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

  // Worker 3 — extraction : action critique → validation humaine (approval).
  // Le run est suspendu (awaiting_approval), puis repris nativement (resuming).
  events.push({ t: "worker.start", at: tick(40), task_id: "t3", role: "data_extractor" });
  events.push({ t: "tool.start", at: tick(15), task_id: "t3", role: "data_extractor", tool: "run_python", args: '{"snippet":"extract(...)"}' });
  events.push({ t: "tool.result", at: tick(80), task_id: "t3", role: "data_extractor", tool: "run_python", status: "ok", summary: "Extraction réussie (1 284 lignes).", duration_ms: 74 });
  events.push({
    t: "worker.approval",
    at: tick(25),
    task_id: "t3",
    role: "data_extractor",
    request_id: "req-1",
    message: "Écriture du fichier de sortie hors répertoire de travail.",
    approval: {
      tool: "write_file",
      reason: "Écriture hors sandbox : validation humaine requise.",
      args_hash: "8f2a71c4e9b05d3a6c8f2a71c4e9b05d",
    },
  });

  // Reprise native (FSM : awaiting_approval → resuming) : le worker 3 est
  // re-dispatché avec `resume_request_id`, l'action approuvée est rejouée.
  events.push({ t: "resuming", at: tick(120), request_id: "req-1", task_id: "t3", role: "data_extractor" });
  events.push({ t: "worker.start", at: tick(30), task_id: "t3", role: "data_extractor", subtask: "Écriture du fichier consolidé" });
  events.push({ t: "tool.start", at: tick(15), task_id: "t3", role: "data_extractor", tool: "write_file", args: '{"path":"out/consolidated.csv"}' });
  events.push({ t: "tool.result", at: tick(90), task_id: "t3", role: "data_extractor", tool: "write_file", status: "ok", summary: "Fichier écrit (88,4 ko).", duration_ms: 84 });
  events.push({ t: "worker.result", at: tick(25), task_id: "t3", role: "data_extractor", summary: "Extraction consolidée et écrite.", duration_ms: 190 });

  // Worker 4 — rédaction (écriture bloquée par la policy : synthèse dégradée)
  events.push({ t: "worker.start", at: tick(40), task_id: "t4", role: "report_writer" });
  events.push({ t: "tool.start", at: tick(15), task_id: "t4", role: "report_writer", tool: "write_file", args: '{"path":"rapport.md"}' });
  events.push({ t: "tool.result", at: tick(60), task_id: "t4", role: "report_writer", tool: "write_file", status: "error", summary: "Permission refusée (écriture hors sandbox).", duration_ms: 58 });
  events.push({ t: "worker.error", at: tick(20), task_id: "t4", role: "report_writer", error_code: "tool_not_allowed", message: "Écriture bloquée par la policy." });

  events.push({ t: "synthesizing", at: tick(60), worker_errors: 1 });
  events.push({
    t: "done",
    at: tick(120),
    status: "completed",
    answer:
      "Synthèse : la dérive climatique est documentée par plusieurs sources convergentes, " +
      "les données ont été extraites puis consolidées après validation (reprise native). " +
      "La rédaction du rapport a été bloquée par la policy — synthèse dégradée.",
    duration_ms: 780,
  });

  return events;
}