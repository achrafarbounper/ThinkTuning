/**
 * Trace temps réel de l orchestration multi-agents (mode « Multi-agents »).
 *
 * Affiche, au fil des événements SSE du superviseur :
 *  - le plan validé (sous-tâches assignées à des rôles),
 *  - l état de chaque worker : en cours (spinner), succès (résumé) ou erreur.
 *
 * Les sous-tâches et résumés proviennent du LLM : ils sont rendus en Markdown
 * EN LIGNE (MarkdownInline — gras, italique, code, liens) pour rester lisibles
 * dans des lignes compactes, sans blocs <p>/<ul> qui casseraient la trace.
 */

import { MarkdownInline } from './markdown';
import type {
  MultiAgentPlanTask,
  MultiAgentWorkerState,
  MultiWorkerStatus,
} from './types';
import type {
  MultiAgentTraceState,
  TraceApprovalEntry,
  TraceToolEntry,
} from './mcpTrace';

interface MultiAgentTraceProps {
  /** Plan validé par le superviseur (événement agent.plan). */
  plan?: MultiAgentPlanTask[];
  /** État courant des workers (événements agent.worker.*). */
  workers?: MultiAgentWorkerState[];
  notice?: string;
  /**
   * Trace ENRICHIE (L3 — SCRUM-154) : run_id durable, outils observés,
   * intention détectée, workers filtrés (skipped) et actions d'approbation.
   * Présente uniquement pour les tours MCP (trace partagée persistée).
   */
  trace?: MultiAgentTraceState;
}

const STATUS_LABELS: Record<MultiWorkerStatus, string> = {
  running: 'En cours',
  ok: 'Terminé',
  error: 'Échec',
  awaiting_approval: 'Validation requise',
};

/** Sous-tâche worker unique (ligne de la trace). */
function WorkerRow({ worker }: { worker: MultiAgentWorkerState }) {
  const subtask =
    worker.subtask || worker.summary || worker.message || 'sous-tâche en cours';

  return (
    <li className={`multi-agent-trace__worker multi-agent-trace__worker--${worker.status}`}>
      <span className="multi-agent-trace__status" aria-hidden="true">
        {worker.status === 'running' ? '⟳' : worker.status === 'ok' ? '✓' : worker.status === 'awaiting_approval' ? '⏳' : '✕'}
      </span>
      <code className="multi-agent-trace__role">{worker.role}</code>
      <span className="multi-agent-trace__subtask" title={subtask}>
        <MarkdownInline content={subtask} />
      </span>
      <span className="multi-agent-trace__state">
        {STATUS_LABELS[worker.status]}
        {worker.durationMs !== undefined &&
          worker.durationMs > 0 &&
          ` · ${(worker.durationMs / 1000).toFixed(1)} s`}
      </span>
    </li>
  );
}

/** Ligne compacte « outil observé » de la timeline (L3 — enrichissement). */
function ToolRow({ entry }: { entry: TraceToolEntry }) {
  const done = entry.event !== 'tool_start' && entry.status !== 'running';
  const failed = entry.status === 'error';
  return (
    <li
      className={`multi-agent-trace__tool multi-agent-trace__tool--${failed ? 'error' : done ? 'ok' : 'running'}`}
    >
      <span className="multi-agent-trace__status" aria-hidden="true">
        {failed ? '✕' : done ? '✓' : '⟳'}
      </span>
      <code className="multi-agent-trace__tool-name">{entry.tool}</code>
      <span className="multi-agent-trace__state">
        {done ? 'Exécuté' : 'En cours'}
        {entry.duration_ms !== undefined &&
          entry.duration_ms > 0 &&
          ` · ${(entry.duration_ms / 1000).toFixed(1)} s`}
      </span>
    </li>
  );
}

/** Ligne « approbation HITL » (L3 — actions d'approbation dans la trace). */
function ApprovalRow({ entry }: { entry: TraceApprovalEntry }) {
  const label = entry.tool ?? 'outil inconnu';
  return (
    <li className="multi-agent-trace__approval">
      <span className="multi-agent-trace__status" aria-hidden="true">⏳</span>
      <code className="multi-agent-trace__role">{label}</code>
      <span className="multi-agent-trace__subtask" title={entry.reason}>
        Validation humaine requise{entry.reason ? ` — ${entry.reason}` : ''}
      </span>
    </li>
  );
}

/** Bloc « Orchestration multi-agents » inséré au-dessus de la bulle de réponse. */
export function MultiAgentTrace({ plan, workers, notice, trace }: MultiAgentTraceProps) {
  const hasPlan = Boolean(plan && plan.length > 0);
  const hasWorkers = Boolean(workers && workers.length > 0);
  const hasTrace = Boolean(
    trace &&
      (trace.runId ||
        trace.tools?.length ||
        trace.skipped?.length ||
        trace.approvals?.length ||
        trace.intent),
  );
  if (!hasPlan && !hasWorkers && !notice && !hasTrace) return null;

  const tools = trace?.tools ?? [];
  const skipped = trace?.skipped ?? [];
  const approvals = trace?.approvals ?? [];

  return (
    <div className="multi-agent-trace" data-testid="multi-agent-trace">
      <div className="multi-agent-trace__header">
        <span className="multi-agent-trace__badge">Orchestration multi-agents</span>
        {hasPlan && (
          <span className="multi-agent-trace__count">
            {plan!.length} sous-tâche{plan!.length > 1 ? 's' : ''}
          </span>
        )}
        {trace?.runId && (
          <code className="multi-agent-trace__run-id" title="Identifiant durable du run (reprise / replay)">
            run {trace.runId}
          </code>
        )}
      </div>

      {notice && (
        <p className="multi-agent-trace__notice" role="status">
          {notice}
        </p>
      )}
      {trace?.intent && (
        <p className="multi-agent-trace__intent" role="status">
          Intention détectée : <strong>{trace.intent}</strong>
        </p>
      )}

      {hasPlan && (
        <ol className="multi-agent-trace__plan">
          {plan!.map((task) => (
            <li key={task.task_id} className="multi-agent-trace__plan-item">
              <code className="multi-agent-trace__role">{task.role}</code>
              <span className="multi-agent-trace__subtask">
                <MarkdownInline content={task.subtask} />
              </span>
            </li>
          ))}
        </ol>
      )}

      {tools.length > 0 && (
        <ul className="multi-agent-trace__tools">
          {tools.map((entry, index) => (
            <ToolRow key={`tool-${index}`} entry={entry} />
          ))}
        </ul>
      )}

      {skipped.length > 0 && (
        <ul className="multi-agent-trace__skipped">
          {skipped.map((entry, index) => (
            <li key={`skip-${index}`}>
              <span aria-hidden="true">⤼</span> worker {entry.worker_id ?? '?'} ignoré
              {entry.reason ? ` — ${entry.reason}` : ''}
            </li>
          ))}
        </ul>
      )}

      {approvals.length > 0 && (
        <ul className="multi-agent-trace__approvals">
          {approvals.map((entry, index) => (
            <ApprovalRow key={`approval-${index}`} entry={entry} />
          ))}
        </ul>
      )}

      {hasWorkers && (
        <ul className="multi-agent-trace__workers">
          {workers!.map((worker) => (
            <WorkerRow key={worker.task_id} worker={worker} />
          ))}
        </ul>
      )}
    </div>
  );
}
