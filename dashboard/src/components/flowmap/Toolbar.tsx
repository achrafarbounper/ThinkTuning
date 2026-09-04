/**
 * Toolbar.tsx — Barre supérieure du Flow Map.
 * Onglets de mode (Live / Replay / Heatmap), commande du run (prompt,
 * lancer), bouton de démo, contrôles de replay (lecture, vitesse, scrub) et
 * filtres (agent / outil / statut).
 */

import type { FlowMode, FlowSource } from "./types";

export interface FlowFilters {
  agent: string;
  tool: string;
  status: string;
  period: string; // « all » | « live » (session en cours)
}

export const NO_FILTER = "__all__";

interface ToolbarProps {
  mode: FlowMode;
  setMode: (m: FlowMode) => void;
  /** Source du run live : orchestration multi-agents ou noyau v2. */
  source: FlowSource;
  setSource: (s: FlowSource) => void;
  prompt: string;
  setPrompt: (p: string) => void;
  onRunLive: () => void;
  onLoadDemo: () => void;
  running: boolean;
  enabled: boolean;
  replayPlaying: boolean;
  onReplayToggle: () => void;
  speed: number;
  setSpeed: (s: number) => void;
  replayProgress: number; // 0..1 (position dans la timeline du replay)
  onSeek: (p: number) => void;
  filters: FlowFilters;
  setFilters: (f: FlowFilters) => void;
  agents: string[];
  tools: string[];
}

export function Toolbar({
  mode,
  setMode,
  source,
  setSource,
  prompt,
  setPrompt,
  onRunLive,
  onLoadDemo,
  running,
  enabled,
  replayPlaying,
  onReplayToggle,
  speed,
  setSpeed,
  replayProgress,
  onSeek,
  filters,
  setFilters,
  agents,
  tools,
}: ToolbarProps) {
  const set = (k: keyof FlowFilters, v: string) => setFilters({ ...filters, [k]: v });
  const live = mode === "live";
  const replay = mode === "replay";

  return (
    <div className="ftoolbar">
      <div className="ftoolbar__row">
        <div className="fmode" role="tablist" aria-label="Mode d'affichage">
          {(
            [
              ["live", "● Live"],
              ["replay", "▶ Replay"],
              ["heatmap", "🌡 Heatmap"],
            ] as Array<[FlowMode, string]>
          ).map(([m, label]) => (
            <button
              key={m}
              type="button"
              role="tab"
              aria-selected={mode === m}
              className={`fmode__tab${mode === m ? " is-active" : ""}`}
              onClick={() => setMode(m)}
            >
              {label}
            </button>
          ))}
        </div>

        {live && (
          <form
            className="frun"
            onSubmit={(e) => {
              e.preventDefault();
              if (enabled && !running) onRunLive();
            }}
          >
            <select
              className="frun__source"
              value={source}
              onChange={(e) => setSource(e.target.value as FlowSource)}
              aria-label="Source du run : orchestration multi-agents ou noyau v2"
              title="Orchestration : superviseur + workers spécialisés. Noyau v2 : agent unique (Intent → Plan → Policy → Budget → Action)."
            >
              <option value="multi">Multi-agents</option>
              <option value="core">Noyau v2</option>
            </select>
            <input
              type="text"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder={
                source === "core"
                  ? "Tâche pour le noyau agentique (optionnel)"
                  : "Tâche globale pour l'orchestrateur (optionnel)"
              }
              aria-label={
                source === "core" ? "Prompt du noyau agentique" : "Prompt de l'orchestrateur"
              }
            />
            <button type="submit" className="frun__go" disabled={running}>
              {running ? "En cours… / Arrêter" : "Lancer le run"}
            </button>
            <button type="button" className="frun__demo" onClick={onLoadDemo} disabled={running}>
              Démo
            </button>
          </form>
        )}

        {replay && (
          <div className="freplay">
            <button
              type="button"
              className="freplay__btn"
              onClick={onReplayToggle}
              disabled={!enabled}
              aria-label={replayPlaying ? "Pause du replay" : "Lancer le replay"}
            >
              {replayPlaying ? "⏸ Pause" : "⏯ Replay"}
            </button>
            <div className="freplay__speeds" role="group" aria-label="Vitesse de replay">
              {[1, 2, 4].map((s) => (
                <button
                  key={s}
                  type="button"
                  className={speed === s ? "is-active" : ""}
                  onClick={() => setSpeed(s)}
                >
                  {s}×
                </button>
              ))}
            </div>
            <input
              type="range"
              className="freplay__scrub"
              min={0}
              max={100}
              value={Math.round(replayProgress * 100)}
              onChange={(e) => onSeek(Number(e.target.value) / 100)}
              aria-label="Position du replay"
            />
            <span className="freplay__pos">{Math.round(replayProgress * 100)}%</span>
          </div>
        )}
      </div>

      <div className="ftoolbar__row ftoolbar__row--filters">
        <label className="ffilter">
          Agent
          <select value={filters.agent} onChange={(e) => set("agent", e.target.value)}>
            <option value={NO_FILTER}>Tous</option>
            {agents.map((a) => (
              <option key={a} value={a}>{a}</option>
            ))}
          </select>
        </label>
        <label className="ffilter">
          Outil
          <select value={filters.tool} onChange={(e) => set("tool", e.target.value)}>
            <option value={NO_FILTER}>Tous</option>
            {tools.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </label>
        <label className="ffilter">
          Statut
          <select value={filters.status} onChange={(e) => set("status", e.target.value)}>
            <option value={NO_FILTER}>Tous</option>
            <option value="running">En cours</option>
            <option value="ok">Succès</option>
            <option value="error">Erreur</option>
          </select>
        </label>
        <span className="ftoolbar__zoomhint">Molette = zoom · Glisser = déplacer · Double-clic = cadrer</span>
      </div>
    </div>
  );
}