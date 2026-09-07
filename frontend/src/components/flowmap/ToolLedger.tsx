/**
 * ToolLedger.tsx — « Journal des outils » : overlay du canvas (Agent Flow Map).
 *
 * Liste complète des outils exécutés, dans l'ordre d'appel, avec leurs
 * métadonnées (statut, durée, instant, arguments, sortie) et leur relation
 * avec les agents (chip du rôle exécutant). Clic sur une ligne → sélection de
 * l'arc correspondant dans le graphe (fiche complète au panneau latéral) ;
 * clic sur l'agent → sélection de son nœud. Le journal reçu est déjà filtré
 * par la page (mêmes règles que la vue canvas).
 */

import { useState } from "react";
import { classifyRole } from "./types";
import type { ToolCallRecord } from "./types";

interface ToolLedgerProps {
  records: ToolCallRecord[];
  /** Arc actuellement sélectionné dans le graphe (ligne surlignée). */
  selectedEdgeId: string | null;
  /** Sélection de l'arc d'un appel (synchronisée avec le canvas). */
  onSelectEdge: (edgeId: string) => void;
  /** Sélection du nœud agent exécutant. */
  onSelectNode: (nodeId: string) => void;
}

const STATUS_LABEL: Record<ToolCallRecord["status"], string> = {
  running: "En cours",
  ok: "Succès",
  error: "Erreur",
};

export function ToolLedger({ records, selectedEdgeId, onSelectEdge, onSelectNode }: ToolLedgerProps) {
  const [collapsed, setCollapsed] = useState(false);
  const errors = records.filter((r) => r.status === "error").length;

  return (
    <section
      className={`fledger${collapsed ? " fledger--collapsed" : ""}`}
      aria-label="Journal des outils exécutés"
      // L'overlay ne doit pas piloter la toile (pan/zoom/double-clic cadrer).
      onPointerDown={(e) => e.stopPropagation()}
      onDoubleClick={(e) => e.stopPropagation()}
      onWheel={(e) => e.stopPropagation()}
    >
      <header className="fledger__head">
        <button
          type="button"
          className="fledger__toggle"
          onClick={() => setCollapsed((c) => !c)}
          aria-expanded={!collapsed}
          title={collapsed ? "Afficher le journal des outils" : "Replier le journal des outils"}
        >
          <span className="fledger__chevron" aria-hidden="true">{collapsed ? "▸" : "▾"}</span>
          <span className="fledger__title">Journal des outils</span>
          <span className="fledger__count">{records.length}</span>
          {errors > 0 && <span className="fledger__count fledger__count--error">{errors} err.</span>}
        </button>
      </header>

      {!collapsed &&
        (records.length === 0 ? (
          <p className="fledger__empty">Aucun outil exécuté pour le moment.</p>
        ) : (
          <ol className="fledger__list">
            {records.map((r) => (
              <LedgerRow
                key={`call-${r.seq}`}
                record={r}
                selected={
                  selectedEdgeId != null &&
                  (r.resultEdgeId === selectedEdgeId || r.startEdgeId === selectedEdgeId)
                }
                onSelectEdge={onSelectEdge}
                onSelectNode={onSelectNode}
              />
            ))}
          </ol>
        ))}
    </section>
  );
}

interface LedgerRowProps {
  record: ToolCallRecord;
  selected: boolean;
  onSelectEdge: (edgeId: string) => void;
  onSelectNode: (nodeId: string) => void;
}

function LedgerRow({ record, selected, onSelectEdge, onSelectNode }: LedgerRowProps) {
  const [expanded, setExpanded] = useState(false);
  const agent = classifyRole(record.role);
  // Arc sélectionné : le « résultat » porte le bilan complet (input + output
  // + durée) ; l'arc « départ » prend le relais tant que l'appel est ouvert.
  const edgeId = record.resultEdgeId ?? record.startEdgeId;

  return (
    <li
      className={["fledger__item", selected ? "is-selected" : "", record.status === "error" ? "is-error" : ""]
        .filter(Boolean)
        .join(" ")}
    >
      <div className="fledger__row">
        <button
          type="button"
          className="fledger__main"
          onClick={() => onSelectEdge(edgeId)}
          title={`Sélectionner l'arc ${record.tool} (agent ${record.role})`}
        >
          <span className="fledger__seq">#{record.seq}</span>
          <span className={`fdot fdot--${record.status}`} aria-hidden="true" />
          <span className="fledger__tool" style={{ color: record.color }}>
            {record.tool}
          </span>
          <span className="fledger__meta">
            {record.durationMs != null ? formatMs(record.durationMs) : "…"} · {formatAt(record.startedAt)}
          </span>
        </button>
        <button
          type="button"
          className="fledger__agent"
          onClick={() => onSelectNode(`role:${record.role}`)}
          title={`Voir l'agent ${record.role}`}
        >
          <span aria-hidden="true">{agent.icon}</span>
          {record.role}
        </button>
        <button
          type="button"
          className={`fledger__more${expanded ? " is-open" : ""}`}
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          title="Métadonnées de l'appel"
        >
          ▸
        </button>
      </div>

      {expanded && (
        <div className="fledger__details">
          <dl>
            <div>
              <dt>Statut</dt>
              <dd>{STATUS_LABEL[record.status]}</dd>
            </div>
            <div>
              <dt>Sous-tâche</dt>
              <dd>{record.taskId || "—"}</dd>
            </div>
            {record.durationMs != null && (
              <div>
                <dt>Durée</dt>
                <dd>{formatMs(record.durationMs)}</dd>
              </div>
            )}
            <div>
              <dt>Début</dt>
              <dd>{formatAt(record.startedAt)}</dd>
            </div>
          </dl>
          {record.args !== undefined && (
            <>
              <h4>Input</h4>
              <pre>{record.args}</pre>
            </>
          )}
          {record.summary !== undefined && (
            <>
              <h4>Output</h4>
              <pre>{record.summary}</pre>
            </>
          )}
        </div>
      )}
    </li>
  );
}

/** Durée lisible : « 118 ms » ou « 1.2 s ». */
function formatMs(ms: number): string {
  if (!Number.isFinite(ms)) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

/** Instant de l'appel, relatif au début de session : « t+1.2 s ». */
function formatAt(at: number): string {
  return `t+${formatMs(at)}`;
}

