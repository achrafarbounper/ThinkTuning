/**
 * DetailPanel.tsx — Panneau latéral de détails (clic agent ou clic tool).
 *
 * Agent : description du rôle, outils exécutés (noms + durées), volume de
 * sous-tâches, statut, et messages du plan. Outil : input (args), output
 * (summary), durée, et l'arc « agent source → agent cible ».
 */

import { classifyRole } from "./types";
import type { GraphState, Selection } from "./types";

interface DetailPanelProps {
  selected: Selection;
  graph: GraphState;
  onClose: () => void;
}

const STATUS_LABEL: Record<string, string> = {
  ready: "Prêt",
  running: "En cours",
  ok: "Terminé",
  error: "Erreur",
};

export function DetailPanel({ selected, graph, onClose }: DetailPanelProps) {
  if (!selected) return null;

  if (selected.type === "node") {
    return <NodeDetail id={selected.id} graph={graph} onClose={onClose} />;
  }
  return <EdgeDetail id={selected.id} graph={graph} onClose={onClose} />;
}

function NodeDetail({ id, graph, onClose }: { id: string; graph: GraphState; onClose: () => void }) {
  const node = graph.nodes[id];
  if (!node) return null;
  const meta = classifyRole(node.role);
  const isPlanner = node.id === "role:planner";

  // Outils exécutés par cet agent (arcs entrants de type tool, agrégés).
  const tools = Object.values(graph.edges)
    .filter((e) => e.target === id && e.kind === "tool")
    .reduce<Record<string, { count: number; totalMs: number; status: string }>>((acc, e) => {
      const key = e.label;
      const cur = acc[key] ?? { count: 0, totalMs: 0, status: "ok" };
      cur.count += 1;
      cur.totalMs += e.totalDurationMs;
      if (e.status === "error") cur.status = "error";
      acc[key] = cur;
      return acc;
    }, {});

  const subtasks = isPlanner ? graph.plan : graph.plan.filter((p) => p.role === node.role);

  return (
    <aside className="fpanel">
      <div className="fpanel__head">
        <span className="fpanel__icon" aria-hidden="true">{node.icon}</span>
        <div className="fpanel__title">
          <strong>{isPlanner ? "Planificateur / Orchestrateur" : meta.label}</strong>
          <small className="tt-mono">{node.role}</small>
        </div>
        <button type="button" className="fpanel__close" onClick={onClose} aria-label="Fermer">
          ✕
        </button>
      </div>

      <dl className="fpanel__stats">
        <div>
          <dt>Statut</dt>
          <dd>
            <span className={`fdot fdot--${node.status}`} /> {STATUS_LABEL[node.status]}
          </dd>
        </div>
        <div>
          <dt>Outils</dt>
          <dd>{node.toolCount}</dd>
        </div>
        <div>
          <dt>Sous-tâches</dt>
          <dd>{isPlanner ? graph.plan.length : subtasks.length}</dd>
        </div>
      </dl>

      <section className="fpanel__section">
        <h3>Outils exécutés</h3>
        {Object.keys(tools).length === 0 ? (
          <p className="fpanel__empty">Aucun outil exécuté.</p>
        ) : (
          <ul className="fpanel__tools">
            {Object.entries(tools).map(([name, t]) => (
              <li key={name} className={t.status === "error" ? "is-error" : ""}>
                <code>{name}</code>
                <span className="fpanel__tools-meta">
                  {t.status === "error" ? "⚠ " : ""}×{t.count} · {formatMs(t.totalMs / t.count)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {isPlanner && (
        <section className="fpanel__section">
          <h3>Plan (sous-tâches assignées)</h3>
          <ol className="fpanel__plan">
            {graph.plan.map((p) => (
              <li key={p.task_id}>
                <code className="tt-mono">{p.role}</code>
                <span>{p.subtask}</span>
              </li>
            ))}
          </ol>
        </section>
      )}
    </aside>
  );
}

function EdgeDetail({ id, graph, onClose }: { id: string; graph: GraphState; onClose: () => void }) {
  const edge = graph.edges[id];
  if (!edge) return null;
  const src = graph.nodes[edge.source];
  const tgt = graph.nodes[edge.target];

  return (
    <aside className="fpanel">
      <div className="fpanel__head">
        <span className="fpanel__icon" aria-hidden="true">🔧</span>
        <div className="fpanel__title">
          <strong>{edge.kind === "tool" ? edge.label : edge.label}</strong>
          <small className="tt-mono">
            {edge.kind === "tool" ? `tool · ${edge.category}` : `arc · ${edge.kind}`}
          </small>
        </div>
        <button type="button" className="fpanel__close" onClick={onClose} aria-label="Fermer">
          ✕
        </button>
      </div>

      <dl className="fpanel__stats">
        <div>
          <dt>Statut</dt>
          <dd className={edge.status === "error" ? "tt-error" : ""}>{edge.status}</dd>
        </div>
        <div>
          <dt>Occurrences</dt>
          <dd>×{edge.count}</dd>
        </div>
        <div>
          <dt>Durée moy.</dt>
          <dd className="tt-mono">{formatMs(edge.totalDurationMs / edge.count)}</dd>
        </div>
      </dl>

      <div className="fpanel__flow">
        <code className="fpanel__role">{src?.role ?? edge.source}</code>
        <span className="fpanel__arrow">→</span>
        <code className="fpanel__role fpanel__role--target">{tgt?.role ?? edge.target}</code>
      </div>

      {edge.args !== undefined && (
        <section className="fpanel__section">
          <h3>Input</h3>
          <pre className="fpanel__pre">{edge.args}</pre>
        </section>
      )}
      {edge.summary !== undefined && (
        <section className="fpanel__section">
          <h3>Output</h3>
          <pre className="fpanel__pre">{edge.summary}</pre>
        </section>
      )}
    </aside>
  );
}

function formatMs(ms: number): string {
  if (!Number.isFinite(ms)) return "—";
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms)} ms`;
}