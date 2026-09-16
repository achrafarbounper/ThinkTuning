/**
 * McpStatusBadge.tsx — L3 (SCRUM-154)
 * ---------------------------------------------------------------------
 * Badge d'état MCP du chat : exécute un preflight (initialize → ping →
 * tools/list) et expose le diagnostic à l'utilisateur.
 *
 *  - vert  : serveur joignable, handshake OK, N tools visibles ;
 *  - rouge : erreur actionnable (401 clé API, 403 scope, 503 MCP_FIRST,
 *            réseau indisponible…) ;
 *  - gris  : diagnostic en cours ou jamais exécuté.
 *
 * Le clic relance le diagnostic (re-vérification après correction de la
 * config). Le composant est autonome (fetch via mcpClient), testable avec
 * fetch mocké.
 */

import { useCallback, useEffect, useState } from 'react';
import { makeMcpErrorActionable, preflightMcp } from '../../api/mcpClient';
import type { McpPreflightResult } from '../../api/mcpClient';

export type McpStatusTone = 'unknown' | 'checking' | 'ok' | 'error';

interface McpStatusBadgeProps {
  /** Base URL de l'API (Paramètres / VITE_API_URL). */
  baseUrl: string;
  /** Clé API (X-API-Key) — exigée par le transport MCP (fail-closed). */
  apiKey: string;
  /** Déclenche le preflight au montage (défaut : false — à la demande). */
  autoCheck?: boolean;
}

export function McpStatusBadge({ baseUrl, apiKey, autoCheck = false }: McpStatusBadgeProps) {
  const [result, setResult] = useState<McpPreflightResult | null>(null);
  const [checking, setChecking] = useState(false);

  const runCheck = useCallback(async () => {
    setChecking(true);
    try {
      setResult(await preflightMcp({ baseUrl, apiKey: apiKey || undefined }));
    } catch (error) {
      // Le badge ne doit JAMAIS rejeter : une exception du preflight (réseau
      // coupé, réponse illisible, client non configuré) devient un résultat
      // d'erreur ACTIONNABLE rendu dans le badge, au lieu d'un rejet non géré
      // qui masquerait la panne et laisserait le badge muet.
      setResult({
        ok: false,
        error: makeMcpErrorActionable(error, baseUrl),
      });
    } finally {
      setChecking(false);
    }
  }, [baseUrl, apiKey]);

  // Preflight au montage quand autoCheck (ex : activation du mode MCP).
  // Différé d'un tick : évite un setState SYNCHRONE dans l'effet
  // (react-hooks/set-state-in-effect) et laisse le premier rendu peindre
  // l'état « unknown » avant le diagnostic.
  useEffect(() => {
    if (!autoCheck) return;
    const timer = window.setTimeout(() => {
      void runCheck();
    }, 0);
    return () => {
      window.clearTimeout(timer);
    };
  }, [autoCheck, runCheck]);

  const tone: McpStatusTone = checking
    ? 'checking'
    : result === null
      ? 'unknown'
      : result.ok
        ? 'ok'
        : 'error';

  const label =
    tone === 'checking'
      ? 'MCP : diagnostic…'
      : tone === 'ok'
        ? `MCP : OK (${result?.toolCount ?? 0} tools)`
        : tone === 'error'
          ? 'MCP : indisponible'
          : 'MCP : non diagnostiqué';

  const detail = result?.ok
    ? `Protocole ${result.protocolVersion ?? '?'} — serveur ${result.serverName ?? '?'} — ` +
      `${result.toolCount ?? 0} tool(s) visible(s).`
    : result?.error;

  return (
    <span
      className={`mcp-status mcp-status--${tone}`}
      title={detail ?? 'Cliquer pour diagnostiquer la connexion MCP (initialize, ping, tools/list).'}
    >
      <button
        type="button"
        className="mcp-status__button"
        onClick={() => {
          void runCheck();
        }}
        aria-label="Diagnostic MCP"
        title="Diagnostiquer la connexion MCP (initialize → ping → tools/list)"
      >
        <span className="mcp-status__dot" aria-hidden="true" />
        {label}
      </button>
      {tone === 'error' && detail && (
        <span className="mcp-status__error" role="alert">
          {detail}
        </span>
      )}
    </span>
  );
}