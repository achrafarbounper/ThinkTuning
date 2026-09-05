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

interface MultiAgentTraceProps {
  /** Plan validé par le superviseur (événement agent.plan). */
  plan?: MultiAgentPlanTask[];
  /** État courant des workers (événements agent.worker.*). */
  workers?: MultiAgentWorkerState[];
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

/** Bloc « Orchestration multi-agents » inséré au-dessus de la bulle de réponse. */
export function MultiAgentTrace({ plan, workers }: MultiAgentTraceProps) {
  const hasPlan = Boolean(plan && plan.length > 0);
  const hasWorkers = Boolean(workers && workers.length > 0);
  if (!hasPlan && !hasWorkers) return null;

  return (
    <div className="multi-agent-trace" data-testid="multi-agent-trace">
      <div className="multi-agent-trace__header">
        <span className="multi-agent-trace__badge">Orchestration multi-agents</span>
        {hasPlan && (
          <span className="multi-agent-trace__count">
            {plan!.length} sous-tâche{plan!.length > 1 ? 's' : ''}
          </span>
        )}
      </div>

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
