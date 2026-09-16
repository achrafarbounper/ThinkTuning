import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ChatWindow } from './ChatWindow';

const { orchestrateViaMcpStreamMock, preflightMcpMock } = vi.hoisted(() => ({
  orchestrateViaMcpStreamMock: vi.fn(),
  preflightMcpMock: vi.fn(),
}));

vi.mock('../../api/mcpClient', () => ({
  orchestrateViaMcpStream: orchestrateViaMcpStreamMock,
  preflightMcp: preflightMcpMock,
  // Message actionnable mocké fidèle au comportement réel (concat de l'action).
  makeMcpErrorActionable: (error: unknown): string =>
    error instanceof Error ? `${error.message} — Action : (mock)` : String(error),
  // Classe mockée : l'instanceof dans useAssistantTurns doit fonctionner.
  McpTransportError: class McpTransportError extends Error {
    status: number;
    code?: number;
    constructor(message: string, status = 0, code?: number) {
      super(message);
      this.name = 'McpTransportError';
      this.status = status;
      this.code = code;
    }
  },
}));

vi.mock('../../context/useApp', () => ({
  useApp: () => ({ config: { baseUrl: '', apiKey: '' } }),
}));

function mockFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v1/chat/models')) {
        return Promise.resolve(
          new Response(JSON.stringify({ models: [] }), {
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      }
      if (url.endsWith('/api/v1/sessions')) {
        return Promise.resolve(
          new Response(JSON.stringify({ sessions: [] }), {
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    }),
  );
}

function sseResponse(frames: Array<{ event: string; data: unknown }>): Response {
  const payload = frames
    .map(({ event, data }) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
    .concat('data: [DONE]\n\n')
    .join('');
  return new Response(payload, {
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

beforeEach(() => {
  window.localStorage.clear();
  window.requestAnimationFrame = (callback: FrameRequestCallback): number => {
    callback(0);
    return 0;
  };
  mockFetch();
  orchestrateViaMcpStreamMock.mockImplementation(
    async (
      _args: unknown,
      onEvent: (event: {
        kind: 'rpc';
        rpc: { result: { content: Array<{ type: string; text: string }> } };
      }) => void,
    ) => {
      onEvent({
        kind: 'rpc',
        rpc: {
          result: {
            content: [
              {
                type: 'text',
                text: JSON.stringify({ answer: 'Réponse MCP', status: 'completed' }),
              },
            ],
          },
        },
      });
      return { answer: 'Réponse MCP', status: 'completed' };
    },
  );
  // Badge MCP : preflight OK par défaut (diagnostic vert, 2 tools visibles).
  preflightMcpMock.mockResolvedValue({
    ok: true,
    protocolVersion: '2025-06-18',
    serverName: 'thinktuning-mcp',
    toolCount: 2,
    tools: ['orchestrate', 'ping'],
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe('ChatWindow - sélecteur d’orchestration MCP', () => {
  it('active le sélecteur en mode MCP et transmet le sous-mode choisi au transport', async () => {
    render(<ChatWindow />);

    const orchestrationSelect = screen.getByRole('combobox', {
      name: "Mode d'orchestration MCP",
    });

    expect(orchestrationSelect).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: /^MCP$/ }));
    expect(orchestrationSelect).toBeEnabled();

    fireEvent.change(orchestrationSelect, { target: { value: 'mono_agent' } });
    expect(window.localStorage.getItem('thinktuning.mcpAgentMode')).toBe('mono_agent');

    fireEvent.change(screen.getByRole('textbox', { name: 'Votre message' }), {
      target: { value: 'Analyse via MCP' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));

    await waitFor(() => expect(orchestrateViaMcpStreamMock).toHaveBeenCalledTimes(1));
    expect(orchestrateViaMcpStreamMock.mock.calls[0][0]).toMatchObject({
      prompt: 'Analyse via MCP',
      mode: 'mono_agent',
      parallel: false,
    });
  });

  describe('ChatWindow - réponse finale multi-agents', () => {
    it('affiche final_answer lorsque agent.done ne contient pas answer', async () => {
      window.localStorage.setItem('thinktuning.multiAgentMode', 'true');

      const fetchMock = vi.mocked(fetch);
      fetchMock.mockImplementation((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/api/v1/chat/models')) {
          return Promise.resolve(
            new Response(JSON.stringify({ models: [] }), {
              headers: { 'Content-Type': 'application/json' },
            }),
          );
        }
        if (url.endsWith('/api/v1/sessions')) {
          return Promise.resolve(
            new Response(JSON.stringify({ sessions: [] }), {
              headers: { 'Content-Type': 'application/json' },
            }),
          );
        }
        if (url.endsWith('/api/v1/agent/multi/ask/stream')) {
          return Promise.resolve(
            sseResponse([
              {
                event: 'agent.done',
                data: {
                  status: 'completed',
                  final_answer: 'Réponse finale multi-agents',
                },
              },
            ]),
          );
        }
        throw new Error(`Unexpected fetch URL: ${url}`);
      });

      render(<ChatWindow />);
      fireEvent.change(screen.getByRole('textbox', { name: 'Votre message' }), {
        target: { value: 'Analyse multi-agents' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));

      expect(await screen.findByText('Réponse finale multi-agents')).toBeInTheDocument();
    });

    it('affiche le résumé worker si le flux se termine sans agent.done', async () => {
      window.localStorage.setItem('thinktuning.multiAgentMode', 'true');

      const fetchMock = vi.mocked(fetch);
      fetchMock.mockImplementation((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith('/api/v1/chat/models')) {
          return Promise.resolve(
            new Response(JSON.stringify({ models: [] }), {
              headers: { 'Content-Type': 'application/json' },
            }),
          );
        }
        if (url.endsWith('/api/v1/sessions')) {
          return Promise.resolve(
            new Response(JSON.stringify({ sessions: [] }), {
              headers: { 'Content-Type': 'application/json' },
            }),
          );
        }
        if (url.endsWith('/api/v1/agent/multi/ask/stream')) {
          return Promise.resolve(
            sseResponse([
              {
                event: 'agent.worker.result',
                data: {
                  task_id: 'task-2',
                  role: 'ops',
                  status: 'ok',
                  summary: 'Le système a détecté aucun GPU disponible.',
                },
              },
            ]),
          );
        }
        throw new Error(`Unexpected fetch URL: ${url}`);
      });

      render(<ChatWindow />);
      fireEvent.change(screen.getByRole('textbox', { name: 'Votre message' }), {
        target: { value: 'Info GPU' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));

      expect(
        await screen.findByText('Le système a détecté aucun GPU disponible.'),
      ).toBeInTheDocument();
    });

    it("affiche l'erreur MCP quand thinking est suivi de agent.error sans agent.done (P0)", async () => {
      window.localStorage.setItem('thinktuning.mcpMode', 'true');

      orchestrateViaMcpStreamMock.mockImplementationOnce(
        async (_args: unknown, onEvent: (event: unknown) => void) => {
          // Reproduit le flux observé (union discriminée L3) : thinking
          // token-par-token, worker OK puis agent.error — la bulle ne doit
          // PAS rester vide et l'erreur n'est JAMAIS avalée.
          onEvent({ kind: 'thinking', delta: ' about' });
          onEvent({
            kind: 'multi_agent',
            event: 'agent.worker.thinking',
            payload: {
              event: 'agent.worker.thinking',
              task_id: 'task-1',
              role: 'ops',
              thinking: ' about',
              phase: 'lead',
            },
          });
          onEvent({
            kind: 'multi_agent',
            event: 'agent.worker.result',
            payload: {
              event: 'agent.worker.result',
              task_id: 'task-1',
              role: 'ops',
              status: 'ok',
              summary: 'CPU : 8 cœurs détectés.',
            },
          });
          onEvent({
            kind: 'error',
            event: 'agent.error',
            message: 'LLM injoignable pendant la synthèse.',
            payload: {},
          });
          throw new Error(
            'Résultats partiels des workers :\nCPU : 8 cœurs détectés.\n\nLLM injoignable pendant la synthèse.',
          );
        },
      );

      render(<ChatWindow />);
      fireEvent.change(screen.getByRole('textbox', { name: 'Votre message' }), {
        target: { value: 'Info CPU et GPU ?' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));

      expect(
        await screen.findByText(/LLM injoignable pendant la synthèse\./),
      ).toBeInTheDocument();
    });

    it('affiche les résumés workers si le run MCP se termine sans réponse finale (P1)', async () => {
      window.localStorage.setItem('thinktuning.mcpMode', 'true');

      orchestrateViaMcpStreamMock.mockImplementationOnce(
        async (_args: unknown, onEvent: (event: unknown) => void) => {
          onEvent({
            kind: 'multi_agent',
            event: 'agent.worker.result',
            payload: {
              event: 'agent.worker.result',
              task_id: 'task-1',
              role: 'ops',
              status: 'ok',
              summary: 'CPU : 8 cœurs détectés.',
            },
          });
          return { answer: '', status: 'completed' };
        },
      );

      render(<ChatWindow />);
      fireEvent.change(screen.getByRole('textbox', { name: 'Votre message' }), {
        target: { value: 'Info CPU ?' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));

      expect(
        await screen.findByText(/Résultats partiels des workers/),
      ).toBeInTheDocument();
      expect(await screen.findByText(/CPU : 8 cœurs détectés\./)).toBeInTheDocument();
    });
  });

  it('restaure le sous-mode MCP persisté', async () => {
    window.localStorage.setItem('thinktuning.mcpMode', 'true');
    window.localStorage.setItem('thinktuning.mcpAgentMode', 'mono_agent');

    render(<ChatWindow />);

    expect(screen.getByRole('button', { name: /^MCP$/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(
      screen.getByRole('combobox', { name: "Mode d'orchestration MCP" }),
    ).toHaveValue('mono_agent');

    // Le mode MCP restauré monte le badge, qui lance son diagnostic
    // (initialize → ping → tools/list) au tick suivant. On attend sa
    // résolution : les setState « checking → résultat » sont ainsi flushés
    // DANS act(), sinon React signale une mise à jour hors act() survenue
    // après la fin du test.
    expect(await screen.findByText('MCP : OK (2 tools)')).toBeInTheDocument();
  });

  it('affiche une synthèse partielle quand la phase de synthèse expire', async () => {
    orchestrateViaMcpStreamMock.mockImplementationOnce(
      async (_args: unknown, onEvent: (event: unknown) => void) => {
        onEvent({
          kind: 'phase',
          status: 'timeout',
          reason: 'synthesis_timeout',
          payload: {
            event: 'agent.phase',
            phase: 'synthesis',
            status: 'timeout',
            reason: 'synthesis_timeout',
          },
        });
        return {
          answer: 'Résultats partiels disponibles',
          status: 'partial_success',
          phase: 'synthesis_timeout',
          orchestration: { reason: 'synthesis_timeout' },
        };
      },
    );

    render(<ChatWindow />);
    fireEvent.click(screen.getByRole('button', { name: /^MCP$/ }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Votre message' }), {
      target: { value: 'Analyse avec synthèse protégée' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));

    expect(
      await screen.findByText(
        'La synthèse a dépassé son délai ; les résultats partiels sont conservés.',
      ),
    ).toBeInTheDocument();
    expect(await screen.findByText('Résultats partiels disponibles')).toBeInTheDocument();
  });

  it('signale explicitement une deadline globale atteinte', async () => {
    orchestrateViaMcpStreamMock.mockImplementationOnce(
      async (
        _args: unknown,
        onEvent: (event: { phase?: Record<string, unknown> }) => void,
      ) => {
        onEvent({
          phase: {
            event: 'agent.phase',
            phase: 'orchestration',
            status: 'timeout',
            reason: 'orchestration_deadline_reached',
          },
        });
        return {
          answer: 'Résultats collectés avant la deadline',
          status: 'partial_success',
          phase: 'orchestration_deadline_reached',
          orchestration: { reason: 'orchestration_deadline_reached' },
        };
      },
    );

    render(<ChatWindow />);
    fireEvent.click(screen.getByRole('button', { name: /^MCP$/ }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Votre message' }), {
      target: { value: 'Analyse avec deadline' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));

    expect(
      await screen.findByText(
        'La durée maximale de l’orchestration a été atteinte ; les résultats partiels sont conservés.',
      ),
    ).toBeInTheDocument();
    expect(await screen.findByText('Résultats collectés avant la deadline')).toBeInTheDocument();
  });

  it('affiche la carte d’approbation MCP puis relance le MÊME run après validation', async () => {
    // P0 (SCRUM-151) : la carte apparaît (request_id ≠ run_id) et l'approbation
    // relance orchestrateViaMcpStream avec resume_request_id + run_id + task_id.
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v1/chat/models')) {
        return Promise.resolve(
          new Response(JSON.stringify({ models: [] }), {
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      }
      if (url.endsWith('/api/v1/sessions')) {
        return Promise.resolve(
          new Response(JSON.stringify({ sessions: [] }), {
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      }
      if (url.endsWith('/api/v1/agent/approvals/req-1/approve')) {
        return Promise.resolve(
          new Response(JSON.stringify({ status: 'approved' }), {
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    });

    orchestrateViaMcpStreamMock
      .mockImplementationOnce(async () => ({
        answer: 'En attente de validation humaine.',
        status: 'awaiting_approval',
        awaiting_approval: true,
        request_id: 'req-1',
        run_id: 'run-77',
        task_id: 't1',
        approval: {
          tool: 'write_file',
          args: { path: 'x' },
          reason: 'validation humaine requise',
        },
      }))
      .mockImplementationOnce(async () => ({
        answer: 'Run repris et terminé.',
        status: 'completed',
        run_id: 'run-77',
      }));

    render(<ChatWindow />);
    fireEvent.click(screen.getByRole('button', { name: /^MCP$/ }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Votre message' }), {
      target: { value: 'Écris le fichier' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));

    // Carte de validation HITL affichée sur la première réponse MCP.
    expect(await screen.findByText('Validation requise')).toBeInTheDocument();
    expect(screen.getByText(/Approuver et exécuter/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Approuver/ }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/api/v1/agent/approvals/req-1/approve'),
        expect.anything(),
      ),
    );
    // Le MÊME run est relancé : resume_request_id (demande approuvée) ET
    // run_id (run durable) sont transmis ensemble — identifiants distincts.
    await waitFor(() => expect(orchestrateViaMcpStreamMock).toHaveBeenCalledTimes(2));
    expect(orchestrateViaMcpStreamMock.mock.calls[1][0]).toMatchObject({
      prompt: 'Écris le fichier',
      resume_request_id: 'req-1',
      run_id: 'run-77',
      task_id: 't1',
    });
    expect(await screen.findByText('Run repris et terminé.')).toBeInTheDocument();
  });
});

describe('ChatWindow - L3 (SCRUM-154) : badge, trace et repli HTTP', () => {
  it('affiche le badge MCP avec le diagnostic preflight en mode MCP', async () => {
    render(<ChatWindow />);

    // Hors mode MCP : pas de badge.
    expect(screen.queryByText(/MCP : OK/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^MCP$/ }));

    // Le badge apparaît puis affiche le résultat du preflight (OK, 2 tools).
    expect(await screen.findByText('MCP : OK (2 tools)')).toBeInTheDocument();
    expect(preflightMcpMock).toHaveBeenCalledTimes(1);
    expect(preflightMcpMock.mock.calls[0][0]).toMatchObject({ baseUrl: expect.any(String) });
  });

  it('attache la trace enrichie au tour MCP (run_id visible + persistance)', async () => {
    orchestrateViaMcpStreamMock.mockImplementationOnce(
      async (_args: unknown, onEvent: (event: unknown) => void) => {
        // Prélude durable (run_id + curseur) puis un worker OK.
        onEvent({ kind: 'started', run_id: 'run-42', resumed: false, last_sequence: 3 });
        onEvent({
          kind: 'multi_agent',
          event: 'agent.worker.result',
          payload: {
            event: 'agent.worker.result',
            task_id: 'task-1',
            role: 'ops',
            status: 'ok',
            summary: 'Analyse terminée.',
          },
        });
        onEvent({ kind: 'intent', intent: 'analyse', payload: {} });
        return { answer: 'Réponse MCP tracée.', status: 'completed' };
      },
    );

    render(<ChatWindow />);
    fireEvent.click(screen.getByRole('button', { name: /^MCP$/ }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Votre message' }), {
      target: { value: 'Trace-moi ça' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));

    // La bulle porte la trace enrichie (run_id rendu par MultiAgentTrace).
    expect(await screen.findByText('Réponse MCP tracée.')).toBeInTheDocument();
    expect(await screen.findByText(/run run-42/)).toBeInTheDocument();
    expect(screen.getByText(/Intention détectée/)).toBeInTheDocument();

    // La trace partagée est PERSISTÉE (restauration après rechargement).
    const stored = window.localStorage.getItem('thinktuning.multiAgentTrace');
    expect(stored).toBeTruthy();
    expect(JSON.parse(stored as string)).toMatchObject({
      runId: 'run-42',
      intent: 'analyse',
      lastSequence: 3,
    });
    expect((JSON.parse(stored as string).workers ?? []).length).toBe(1);
  });

  it('propose le repli HTTP sur erreur MCP 503 et rejoue via le noyau legacy', async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/v1/chat/models')) {
        return Promise.resolve(
          new Response(JSON.stringify({ models: [] }), {
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      }
      if (url.endsWith('/api/v1/sessions')) {
        return Promise.resolve(
          new Response(JSON.stringify({ sessions: [] }), {
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      }
      // Repli core : stream inconnu (404) puis POST bloquant (réponse JSON).
      if (url.endsWith('/api/v1/agent/ask/core/stream')) {
        return Promise.resolve(new Response('not found', { status: 404 }));
      }
      if (url.endsWith('/api/v1/agent/ask/core')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ response: 'Réponse legacy HTTP.', status: 'completed', model: 'x' }),
            { headers: { 'Content-Type': 'application/json' } },
          ),
        );
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    });

    // Échec transport MCP 503 (MCP_FIRST gèle l'HTTP legacy) → actionable + repli.
    orchestrateViaMcpStreamMock.mockImplementationOnce(async () => {
      const { McpTransportError: MockedTransportError } = (await import(
        '../../api/mcpClient'
      )) as { McpTransportError: new (message: string, status?: number) => Error & { status: number } };
      throw new MockedTransportError('Surface MCP indisponible (503).', 503);
    });

    render(<ChatWindow />);
    fireEvent.click(screen.getByRole('button', { name: /^MCP$/ }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Votre message' }), {
      target: { value: 'Demande bloquée par MCP_FIRST' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));

    // Erreur rendue ACTIONNABLE + bandeau de repli HTTP proposé.
    expect(await screen.findByText(/Action : \(mock\)/)).toBeInTheDocument();
    const resend = await screen.findByRole('button', { name: /Renvoyer via HTTP/ });
    fireEvent.click(resend);

    // La demande est rejouée via le pipeline legacy (noyau v2, HTTP).
    expect(await screen.findByText('Réponse legacy HTTP.')).toBeInTheDocument();
    // Le repli ne propose plus une seconde fois le même renvoi.
    expect(screen.queryByRole('button', { name: /Renvoyer via HTTP/ })).not.toBeInTheDocument();
  });
});
