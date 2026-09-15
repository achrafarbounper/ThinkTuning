import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ChatWindow } from './ChatWindow';

const { orchestrateViaMcpStreamMock } = vi.hoisted(() => ({
  orchestrateViaMcpStreamMock: vi.fn(),
}));

vi.mock('../../api/mcpClient', () => ({
  orchestrateViaMcpStream: orchestrateViaMcpStreamMock,
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
      onEvent: (event: { rpc: { result: { content: Array<{ type: string; text: string }> } } }) => void,
    ) => {
      onEvent({
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
        async (
          _args: unknown,
          onEvent: (event: {
            multi_agent?: Record<string, unknown>;
            thinking_delta?: string;
          }) => void,
        ) => {
          // Reproduit le flux observé : thinking token-par-token puis erreur,
          // sans agent.done — la bulle ne doit PAS rester vide.
          onEvent({ thinking_delta: ' about' });
          onEvent({
            multi_agent: {
              event: 'agent.worker.thinking',
              task_id: 'task-1',
              role: 'ops',
              thinking: ' about',
              phase: 'lead',
            },
          });
          onEvent({
            multi_agent: {
              event: 'agent.worker.result',
              task_id: 'task-1',
              role: 'ops',
              status: 'ok',
              summary: 'CPU : 8 cœurs détectés.',
            },
          });
          onEvent({
            multi_agent: {
              event: 'agent.error',
              message: 'LLM injoignable pendant la synthèse.',
            },
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
        async (
          _args: unknown,
          onEvent: (event: { multi_agent?: Record<string, unknown> }) => void,
        ) => {
          onEvent({
            multi_agent: {
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

  it('restaure le sous-mode MCP persisté', () => {
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
  });

  it('affiche une synthèse partielle quand la phase de synthèse expire', async () => {
    orchestrateViaMcpStreamMock.mockImplementationOnce(
      async (
        _args: unknown,
        onEvent: (event: { phase?: Record<string, unknown> }) => void,
      ) => {
        onEvent({
          phase: {
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
