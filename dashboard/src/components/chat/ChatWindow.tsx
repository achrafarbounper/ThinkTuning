/**
 * Fenêtre de chat complète, façon GitHub Copilot Chat.
 *
 * Gère :
 * - l'historique des messages (bulles utilisateur / IA),
 * - le streaming de la réponse via POST /api/ai (Server-Sent Events),
 * - l'authentification via l'en-tête X-API-Key (config dashboard ou VITE_API_KEY),
 * - le chargement (spinner + curseur clignotant),
 * - le défilement automatique vers le bas (avec respect du scroll manuel),
 * - l'interruption de la génération (AbortController),
 * - la nouvelle session via le bouton « Nouvelle tâche » (réinitialisation).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { UIEvent } from 'react';
import { ChatMessage } from './ChatMessage';
import { ChatInput } from './ChatInput';
import { ChatModelSelector } from './ChatModelSelector';
import { SessionSelector } from './SessionSelector';
import { readNamedSseEvents, readSseEvents } from './streamSse';
import type {
  AgentAskResponse,
  ChatMessageData,
  ChatRequestBody,
  ChatSessionInfo,
  ChatStreamEvent,
  LlmModelInfo,
  LlmModelsResponse,
  MultiAgentPlanTask,
  MultiAgentStreamEvent,
  MultiAgentWorkerState,
  PendingApprovalData,
  StoredMessage,
  ToolCallData,
  ToolCallStatus,
} from './types';
import './chat.css';

/** Endpoint du backend (proxifié par Vite vers l'API FastAPI en développement). */
const AI_ENDPOINT = '/api/ai';

/**
 * Endpoint du mode Agent (POST /api/agent/ask) : contrairement au chat SSE
 * /api/ai, l'agent peut déclencher des outils ; une action à risque renvoie
 * status="awaiting_approval" et attend la décision humaine (approve/reject).
 */
const AGENT_ASK_ENDPOINT = '/api/agent/ask';

/**
 * Variante streaming du mode Agent (POST /api/agent/ask/stream) : mêmes
 * événements temps réel que /api/ai (thinking_delta, delta) PLUS la diffusion
 * des appels d'outils (tool_start / tool_result) et un payload final (« final »)
 * portant le statut du gate (completed / awaiting_approval / rejected).
 * Utilisé en priorité ; repli automatique sur POST /api/agent/ask si absent.
 */
const AGENT_ASK_STREAM_ENDPOINT = '/api/agent/ask/stream';

/** Base des endpoints de validation humaine (approve / reject). */
const APPROVALS_ENDPOINT = '/api/agent/approvals';

/**
 * Endpoint d orchestration multi-agents (POST /api/agent/multi/ask/stream) :
 * le superviseur planifie, dispatche des sous-taches a des workers isoles puis
 * synthetise. Evenements SSE nommes agent.plan / agent.worker.* / agent.done.
 */
const MULTI_ASK_STREAM_ENDPOINT = '/api/agent/multi/ask/stream';

/** Mode SSE demande : les evenements d observabilite (worker.tool) sont filtres. */
const MULTI_SSE_MODE = 'compact';

/** Endpoint des conversations persistées (GET/POST /api/sessions…). */
const SESSIONS_ENDPOINT = '/api/sessions';

/** Clé de persistance de la conversation active (localStorage). */
const CHAT_SESSION_STORAGE_KEY = 'thinktuning.chatSession';

/** Endpoint listant les modèles LLM disponibles (même proxy que /api/ai). */
const MODELS_ENDPOINT = '/api/models';

/** Clé de stockage partagée avec le dashboard (voir CONFIG_STORAGE_KEY dans context/AppContext.jsx). */
const API_CONFIG_STORAGE_KEY = 'thinktuning.apiConfig';

/** Clé de persistance du modèle LLM choisi pour le chat (localStorage). */
const CHAT_MODEL_STORAGE_KEY = 'thinktuning.chatModel';

/** Clé de persistance du mode « Réflexion » (localStorage). */
const THINKING_STORAGE_KEY = 'thinktuning.enableThinking';

/**
 * Clé de persistance du mode « Agent » (localStorage). Dans ce mode, les
 * messages partent vers /api/agent/ask au lieu du chat SSE /api/ai : l'agent
 * peut appeler ses outils, et une action à risque déclenche la carte de
 * validation humaine (auto_approve / approve / reject).
 */
const AGENT_MODE_STORAGE_KEY = 'thinktuning.agentMode';

/**
 * Cle de persistance du mode « Multi-agents » (orchestration superviseur /
 * workers via /api/agent/multi/ask/stream). Mutuellement exclusif avec le
 * mode Agent simple.
 */
const MULTI_MODE_STORAGE_KEY = 'thinktuning.multiAgentMode';

/** Nombre maximal de caractères d'arguments affichés sur la carte d'approbation. */
const APPROVAL_ARGS_PREVIEW_LIMIT = 400;

/** Distance (px) sous laquelle on considère que l'utilisateur « suit » le bas. */
const SCROLL_THRESHOLD_PX = 80;

/** Identifiant unique de message, avec repli pour les navigateurs anciens. */
function createId(): string {
  return typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `msg-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`;
}

function nowIso(): string {
  return new Date().toISOString();
}

/**
 * Récupère la clé API attendue par le backend (en-tête X-API-Key).
 *
 * Source principale : la configuration du dashboard persistée en localStorage
 * (champ « API_KEY côté serveur » du formulaire Configuration) ; repli : la
 * variable d'environnement Vite VITE_API_KEY. Résolue à chaque envoi afin de
 * prendre en compte un changement de configuration sans recharger la page.
 */
function resolveApiKey(): string {
  try {
    const raw = window.localStorage.getItem(API_CONFIG_STORAGE_KEY);
    const apiKey = raw ? (JSON.parse(raw) as { apiKey?: string }).apiKey : undefined;
    if (apiKey) return apiKey;
  } catch {
    /* stockage indisponible ou JSON invalide : on utilise le repli ci-dessous */
  }
  return import.meta.env.VITE_API_KEY ?? '';
}

/** Relit le modèle LLM choisi pour le chat ('' = modèle par défaut serveur). */
function loadStoredChatModel(): string {
  try {
    return window.localStorage.getItem(CHAT_MODEL_STORAGE_KEY) ?? '';
  } catch {
    return '';
  }
}

/** Relit l'état persisté du mode « Réflexion » (désactivé par défaut). */
function loadStoredThinking(): boolean {
  try {
    return window.localStorage.getItem(THINKING_STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

/** Relit l'état persisté du mode « Agent » (désactivé par défaut). */
function loadStoredAgentMode(): boolean {
  try {
    return window.localStorage.getItem(AGENT_MODE_STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

/** Relit l etat persiste du mode « Multi-agents » (desactive par defaut). */
function loadStoredMultiMode(): boolean {
  try {
    return window.localStorage.getItem(MULTI_MODE_STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

/** Relit l'id de la conversation active ('' = aucune / nouvelle). */
function loadStoredSession(): string {
  try {
    return window.localStorage.getItem(CHAT_SESSION_STORAGE_KEY) ?? '';
  } catch {
    return '';
  }
}

/** Persiste (ou efface) l'id de la conversation active. */
function storeSession(id: string): void {
  try {
    if (id) window.localStorage.setItem(CHAT_SESSION_STORAGE_KEY, id);
    else window.localStorage.removeItem(CHAT_SESSION_STORAGE_KEY);
  } catch {
    /* stockage indisponible : la sélection reste valable pour la session */
  }
}

/**
 * Convertit les événements d'outils bruts d'une conversation rechargée en
 * timeline appariée (tool_start -> tool_result), même modèle que le streaming.
 */
function mapStoredToolCalls(
  events: StoredMessage['tool_calls'],
): ToolCallData[] | undefined {
  if (!events || events.length === 0) return undefined;
  const calls: ToolCallData[] = [];
  for (const raw of events) {
    const event = raw as Record<string, string | number>;
    const tool = String(event.tool ?? '?');
    if (event.event === 'tool_start') {
      calls.push({
        tool,
        args: typeof event.args === 'string' ? event.args : undefined,
        status: 'running',
      });
    } else if (event.event === 'tool_result') {
      const status = (event.status as ToolCallStatus) || 'ok';
      const durationMs = Number(event.duration_ms);
      const target = [...calls]
        .reverse()
        .find((call) => call.tool === tool && call.status === 'running');
      if (target) {
        target.status = status;
        target.summary = typeof event.summary === 'string' ? event.summary : undefined;
        target.durationMs = Number.isFinite(durationMs) ? durationMs : undefined;
      } else {
        calls.push({
          tool,
          status,
          summary: typeof event.summary === 'string' ? event.summary : undefined,
          durationMs: Number.isFinite(durationMs) ? durationMs : undefined,
        });
      }
    }
  }
  return calls.length > 0 ? calls : undefined;
}

/** Aperçu compact des arguments d'un appel pour la carte d'approbation. */
function formatArgsPreview(args: Record<string, unknown> | undefined): string {
  if (!args || Object.keys(args).length === 0) return '{}';
  const json = JSON.stringify(args, null, 2);
  return json.length > APPROVAL_ARGS_PREVIEW_LIMIT
    ? `${json.slice(0, APPROVAL_ARGS_PREVIEW_LIMIT)}…`
    : json;
}

/**
 * Extrait le message d'erreur FastAPI (`detail`) d'une réponse non-OK.
 * Repli sur un message générique si le corps n'est pas JSON ou sans `detail`.
 */
async function apiErrorMessage(response: Response): Promise<string> {
  const generic = `Le serveur a répondu ${response.status} (${response.statusText})`;
  try {
    const data = await response.json();
    const detail = (data as { detail?: unknown })?.detail;
    if (typeof detail === 'string' && detail) return detail;
  } catch {
    /* corps non-JSON : on garde le message générique */
  }
  return generic;
}

export function ChatWindow() {
  const [messages, setMessages] = useState<ChatMessageData[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [stickToBottom, setStickToBottom] = useState(true);

  // Sélecteur de modèle LLM : liste fournie par GET /api/models, choix
  // persisté en localStorage pour survivre au rechargement de la page.
  const [llmModels, setLlmModels] = useState<LlmModelInfo[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>(loadStoredChatModel);
  const [modelsLoading, setModelsLoading] = useState(true);
  // Mode « Réflexion » : transmis au backend (enable_thinking) pour chaque
  // message et persisté en localStorage comme le modèle sélectionné.
      const [enableThinking, setEnableThinking] = useState<boolean>(loadStoredThinking);
  const [modelsError, setModelsError] = useState('');
  // Mode « Agent » : les messages passent par /api/agent/ask (l'agent peut
  // appeler ses outils). Une action à risque affiche la carte d'approbation.
  const [agentMode, setAgentMode] = useState<boolean>(loadStoredAgentMode);
  // Mode « Multi-agents » : les messages partent vers /api/agent/multi/ask/
  // stream (superviseur : plan -> dispatch -> synthese) et la trace temps reel
  // (plan + workers) est affichee dans la bulle de reponse.
  const [multiMode, setMultiMode] = useState<boolean>(loadStoredMultiMode);
  // Demande en attente de décision humaine (approve / reject), le cas échéant.
  const [pendingApproval, setPendingApproval] = useState<PendingApprovalData | null>(null);

  // Conversations persistées côté serveur (GET/POST /api/sessions) : la
  // conversation active est sélectionnée via le menu de l'en-tête ; '' =
  // aucune (création à la volée au premier message envoyé).
  const [sessions, setSessions] = useState<ChatSessionInfo[]>([]);
  const [sessionId, setSessionId] = useState<string>(loadStoredSession);

  const listRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController>(null);
  // Carte de validation (role="alertdialog") : le focus y est déplacé à son
  // apparition pour que les technologies d'assistance et le clavier la détectent.
  const approvalRef = useRef<HTMLDivElement>(null);

  // Miroir de l'état pour lire un historique à jour dans les callbacks asynchrones.
  // La synchronisation se fait dans un effet : muter une ref pendant le rendu
  // est interdit (règle react-hooks/refs) et non fiable en rendu concurrent.
  const messagesRef = useRef(messages);
  useEffect(() => {
    messagesRef.current = messages;
  });

  // Focus la carte d'approbation quand elle apparaît (validation requise).
  useEffect(() => {
    if (pendingApproval && approvalRef.current) {
      approvalRef.current.focus();
    }
  }, [pendingApproval]);

  // Scroll automatique vers le bas à chaque nouveau message / token,
  // uniquement si l'utilisateur n'a pas remonté manuellement la conversation.
  useEffect(() => {
    const element = listRef.current;
    if (element && stickToBottom) {
      element.scrollTop = element.scrollHeight;
    }
  }, [messages, stickToBottom]);

  // Chargement initial des modèles LLM disponibles (GET /api/models).
  useEffect(() => {
    let cancelled = false;

    const loadModels = async () => {
      setModelsLoading(true);
      setModelsError('');
      try {
        // Route protégée par require_api_key côté backend : même en-tête
        // que POST /api/ai.
        const headers: Record<string, string> = {};
        const apiKey = resolveApiKey();
        if (apiKey) headers['X-API-Key'] = apiKey;

        const response = await fetch(MODELS_ENDPOINT, { headers });
        if (!response.ok) {
          throw new Error(await apiErrorMessage(response));
        }
        const data = (await response.json()) as LlmModelsResponse;
        if (!cancelled) setLlmModels(data.models ?? []);
      } catch (error) {
        if (!cancelled) {
          setModelsError(error instanceof Error ? error.message : String(error));
        }
      } finally {
        if (!cancelled) setModelsLoading(false);
      }
    };

    void loadModels();
    return () => {
      cancelled = true;
    };
  }, []);

  // --- Conversations persistées (/api/sessions) --------------------------------

  // Charge la liste des conversations au montage du composant.
  useEffect(() => {
    let cancelled = false;
    const loadSessions = async (): Promise<void> => {
      const headers: Record<string, string> = {};
      const apiKey = resolveApiKey();
      if (apiKey) headers['X-API-Key'] = apiKey;
      try {
        const response = await fetch(SESSIONS_ENDPOINT, { headers });
        if (!response.ok) {
          throw new Error(await apiErrorMessage(response));
        }
        const data = (await response.json()) as { sessions: ChatSessionInfo[] };
        if (!cancelled) setSessions(data.sessions ?? []);
      } catch {
        /* échec silencieux : le chat reste utilisable hors persistance */
        if (!cancelled) setSessions([]);
      }
    };
    void loadSessions();
    return () => {
      cancelled = true;
    };
  }, []);

  // Persiste l'id de la conversation active (localStorage) quand il change.
  useEffect(() => {
    storeSession(sessionId);
  }, [sessionId]);

  /** Charge les messages d'une conversation existante et la rend active. */
  const selectSession = useCallback(
    async (id: string): Promise<void> => {
      abortRef.current?.abort();
      const headers: Record<string, string> = {};
      const apiKey = resolveApiKey();
      if (apiKey) headers['X-API-Key'] = apiKey;
      try {
        const response = await fetch(`${SESSIONS_ENDPOINT}/${id}/messages`, { headers });
        if (!response.ok) throw new Error(await apiErrorMessage(response));
        const stored = (await response.json()) as { messages: StoredMessage[] };
        const storedMessages = stored.messages ?? [];
        setMessages(
          storedMessages.map(
            (message, index): ChatMessageData => ({
              id: `s-${index + 1}`,
              role: message.role,
              content: message.content ?? '',
              createdAt: message.created_at ?? nowIso(),
              thinking: message.role === 'assistant' ? (message.content ?? '') : undefined,
              toolCalls:
                message.role === 'assistant'
                  ? mapStoredToolCalls(message.tool_calls)
                  : undefined,
            }),
          ),
        );
        setSessionId(id);
        setPendingApproval(null);
        setStickToBottom(true);
      } catch {
        /* erreur non bloquante : on garde la conversation courante */
      }
    },
    [],
  );

  /** Crée une nouvelle conversation vide et la sélectionne (mode « Nouvelle tâche »). */
  const createSession = useCallback(async (): Promise<void> => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const apiKey = resolveApiKey();
    if (apiKey) headers['X-API-Key'] = apiKey;
    try {
      const response = await fetch(SESSIONS_ENDPOINT, {
        method: 'POST',
        headers,
        body: JSON.stringify({ title: 'Nouvelle tâche' }),
      });
      if (!response.ok) throw new Error(await apiErrorMessage(response));
      const created = (await response.json()) as ChatSessionInfo;
      setSessionId(created.id);
      setSessions((previous) => [created, ...previous]);
      setMessages([]);
      setPendingApproval(null);
      setStickToBottom(true);
    } catch {
      /* repli hors persistance : réinitialisation locale uniquement */
      setSessionId('');
      setMessages([]);
      setPendingApproval(null);
      setStickToBottom(true);
    }
  }, []);

  /** Change le modèle LLM utilisé par les prochains messages du chat. */
  const handleModelChange = useCallback((modelName: string) => {
    setSelectedModel(modelName);
    try {
      window.localStorage.setItem(CHAT_MODEL_STORAGE_KEY, modelName);
    } catch {
      /* stockage indisponible : la sélection reste valable pour la session */
    }
  }, []);

  /** Active/désactive le mode « Réflexion » et persiste le choix. */
  const handleThinkingToggle = useCallback(() => {
    setEnableThinking((previous) => {
      const next = !previous;
      try {
        window.localStorage.setItem(THINKING_STORAGE_KEY, String(next));
      } catch {
        /* stockage indisponible : le choix reste valable pour la session */
      }
      return next;
    });
  }, []);

  /**
   * Active/désactive le mode « Agent » et persiste le choix. Quitter le mode
   * annule la carte d'approbation affichée (le backend reste maître de la
   * demande, qui expire seule côté store).
   */
  const handleAgentToggle = useCallback(() => {
    setAgentMode((previous) => {
      const next = !previous;
      try {
        window.localStorage.setItem(AGENT_MODE_STORAGE_KEY, String(next));
      } catch {
        /* stockage indisponible : le choix reste valable pour la session */
      }
      return next;
    });
    setPendingApproval(null);
  }, []);

  /**
   * Bascule le mode « Multi-agents » (et desactive le mode Agent simple :
   * les deux routages de message sont mutuellement exclusifs).
   */
  const handleMultiToggle = useCallback(() => {
    setMultiMode((previous) => {
      const next = !previous;
      try {
        window.localStorage.setItem(MULTI_MODE_STORAGE_KEY, String(next));
      } catch {
        /* stockage indisponible : le choix reste valable pour la session */
      }
      return next;
    });
    if (!multiMode) {
      setAgentMode(false);
      try {
        window.localStorage.setItem(AGENT_MODE_STORAGE_KEY, 'false');
      } catch {
        /* idem */
      }
    }
  }, [multiMode]);

  const handleScroll = (event: UIEvent<HTMLDivElement>) => {
    const element = event.currentTarget;
    const distanceFromBottom = element.scrollHeight - element.scrollTop - element.clientHeight;
    setStickToBottom(distanceFromBottom < SCROLL_THRESHOLD_PX);
  };

  const scrollToBottom = () => {
    const element = listRef.current;
    if (element) {
      element.scrollTop = element.scrollHeight;
    }
    setStickToBottom(true);
  };

  /** Ajoute un fragment de texte au message en cours de streaming. */
  const appendDelta = useCallback((id: string, delta: string) => {
    setMessages((previous) =>
      previous.map((message) =>
        message.id === id ? { ...message, content: message.content + delta } : message,
      ),
    );
  }, []);

  /** Ajoute un fragment de réflexion au message en cours de streaming. */
  const appendThinkingDelta = useCallback((id: string, delta: string) => {
    setMessages((previous) =>
      previous.map((message) =>
        message.id === id
          ? { ...message, thinking: (message.thinking ?? '') + delta, thinkingStreaming: true }
          : message,
      ),
    );
  }, []);

  /** Modifie certains champs d'un message (fin de streaming, erreur…). */
  const patchMessage = useCallback((id: string, patch: Partial<ChatMessageData>) => {
    setMessages((previous) =>
      previous.map((message) => (message.id === id ? { ...message, ...patch } : message)),
    );
  }, []);

  /** Ajoute un appel d'outil « running » à la timeline du message (tool_start). */
  const appendToolCall = useCallback((id: string, call: ToolCallData) => {
    setMessages((previous) =>
      previous.map((message) =>
        message.id === id
          ? { ...message, toolCalls: [...(message.toolCalls ?? []), call] }
          : message,
      ),
    );
  }, []);

  /** Clôture le dernier appel « running » portant le même outil (tool_result). */
  const completeToolCall = useCallback(
    (id: string, result: NonNullable<ChatStreamEvent['tool_result']>) => {
      setMessages((previous) =>
        previous.map((message) => {
          if (message.id !== id || !message.toolCalls?.length) return message;
          const calls = [...message.toolCalls];
          for (let index = calls.length - 1; index >= 0; index -= 1) {
            if (calls[index].status === 'running' && calls[index].tool === result.tool) {
              calls[index] = {
                ...calls[index],
                status: ((result.status as ToolCallStatus) || 'ok') satisfies ToolCallStatus,
                summary: result.summary,
                durationMs: result.duration_ms,
              };
              break;
            }
          }
          return { ...message, toolCalls: calls };
        }),
      );
    },
    [],
  );

  /** Enregistre le plan validé par le superviseur (agent.plan). */
  const setMultiPlan = useCallback(
    (id: string, plan: NonNullable<ChatMessageData['multiPlan']>) => {
      patchMessage(id, { multiPlan: plan });
    },
    [patchMessage],
  );

  /** Démarre un worker dans la trace (agent.worker.start). */
  const startMultiWorker = useCallback(
    (id: string, worker: MultiAgentWorkerState) => {
      setMessages((previous) =>
        previous.map((message) =>
          message.id === id
            ? { ...message, multiWorkers: [...(message.multiWorkers ?? []), worker] }
            : message,
        ),
      );
    },
    [],
  );

  /** Clôture un worker par task_id (agent.worker.result / agent.worker.error). */
  const completeMultiWorker = useCallback(
    (
      id: string,
      task_id: string,
      patch: Partial<MultiAgentWorkerState>,
      finalStatus: MultiAgentWorkerState['status'],
    ) => {
      setMessages((previous) =>
        previous.map((message) => {
          if (message.id !== id || !message.multiWorkers?.length) return message;
          const workers = [...message.multiWorkers];
          for (let index = workers.length - 1; index >= 0; index -= 1) {
            if (workers[index].task_id === task_id && workers[index].status === 'running') {
              workers[index] = {
                ...workers[index],
                ...patch,
                status: finalStatus,
                durationMs:
                  patch.durationMs ?? workers[index].durationMs,
              };
              break;
            }
          }
          return { ...message, multiWorkers: workers };
        }),
      );
    },
    [],
  );

  /** Interrompt proprement la génération en cours. */
  const stopGeneration = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  /**
   * Démarre une nouvelle session (« Nouvelle tâche ») : interrompt la
   * génération éventuellement en cours puis ouvre une conversation à vide.
   * La création côté serveur (POST /api/sessions) permet de retrouver la
   * conversation après rechargement ; en cas d'échec, on se rabat sur une
   * réinitialisation strictement locale.
   */
  const startNewSession = useCallback(() => {
    abortRef.current?.abort();
    void createSession();
  }, [createSession]);

  /**
   * Tour d'agent : POST /api/agent/ask/stream en priorité (temps réel complet
   * : réflexion + appels d'outils + réponse mot à mot + statut final du gate),
   * avec repli automatique sur le POST bloquant historique (/api/agent/ask)
   * quand l'endpoint de streaming n'existe pas (backend antérieur).
   *
   * Les trois statuts du gate auto_approve / approve / reject restent couverts :
   *   - completed         : réponse finale affichée au fil de l'eau ;
   *   - awaiting_approval : carte Approuver / Refuser déclenchée ;
   *   - rejected          : motif du blocage policy affiché au mot pour mot.
   */
  /**
   * Tour multi-agents : POST /api/agent/multi/ask/stream (SSE avec evenements
   * nommes). La trace (plan + workers) est rendue en temps reel par
   * MultiAgentTrace ; la reponse finale (agent.done -> final_answer) remplit
   * la bulle. Une erreur globale (plan invalide, LLM inaccessible) est affichee
   * comme une erreur de message.
   */
  const askMultiAgentTurn = useCallback(
    async (
      assistantId: string,
      prompt: string,
      controller: AbortController,
    ): Promise<void> => {
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      const apiKey = resolveApiKey();
      if (apiKey) headers['X-API-Key'] = apiKey;

      // Contrat backend (schema MultiAskRequest) : champs snake_case.
      const body: Record<string, unknown> = {
        prompt,
        mode: MULTI_SSE_MODE,
        parallel: false,
      };
      if (selectedModel) body.model = selectedModel;

      const response = await fetch(MULTI_ASK_STREAM_ENDPOINT, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error(await apiErrorMessage(response));
      }

      const contentType = response.headers.get('content-type') ?? '';
      if (!(contentType.includes('text/event-stream') && response.body)) {
        // Repli JSON non streame : le backend a repondu d un bloc.
        const data = (await response.json()) as {
          status?: string;
          final_answer?: string;
          message?: string;
          plan?: NonNullable<ChatMessageData['multiPlan']>;
          pending_approvals?: Array<{
            task_id: string;
            role: string;
            request_id: string;
            approval?: { tool?: string; args?: Record<string, unknown>; reason?: string };
          }>;
        };
        if (data.plan) setMultiPlan(assistantId, data.plan);
        if (data.status === 'error') {
          throw new Error(data.message || 'Echec de l orchestration multi-agents.');
        }
        // Validation humaine requise (contrat JSON bloquant) : même traitement
        // que le flux SSE — carte Approuver/Refuser sur la première demande.
        const blockedTask = data.pending_approvals?.[0];
        if (data.status === 'awaiting_approval' && blockedTask?.request_id) {
          const subtask = data.plan?.find((t) => t.task_id === blockedTask.task_id)?.subtask;
          completeMultiWorker(
            assistantId,
            blockedTask.task_id,
            { message: data.final_answer ?? data.message },
            'awaiting_approval',
          );
          setPendingApproval({
            requestId: blockedTask.request_id,
            prompt: subtask ?? prompt,
            tool: blockedTask.approval?.tool ?? 'outil inconnu',
            reason: blockedTask.approval?.reason ?? 'validation humaine requise',
            args: blockedTask.approval?.args,
          });
          return;
        }
        appendDelta(assistantId, data.final_answer ?? data.message ?? '');
        return;
      }

      // Plan local : permet de retrouver le texte d'une sous-tâche (nécessaire
      // pour relancer la sous-tâche bloquée avec resume_request_id).
      let planTasks: MultiAgentPlanTask[] = [];
      for await (const frame of readNamedSseEvents(response.body)) {
        if (frame.data === '[DONE]') break;

        let event: MultiAgentStreamEvent;
        try {
          event = JSON.parse(frame.data) as MultiAgentStreamEvent;
        } catch {
          continue; // charge utile illisible : on ignore (tolerance)
        }

        switch (frame.event) {
          case 'agent.plan':
            if (event.plan?.length) {
              planTasks = event.plan;
              setMultiPlan(assistantId, event.plan);
            }
            break;
          case 'agent.worker.start':
            if (event.task_id) {
              startMultiWorker(assistantId, {
                task_id: event.task_id,
                role: event.role ?? '?',
                subtask: event.plan?.[0]?.subtask,
                status: 'running',
              });
            }
            break;
          case 'agent.worker.result':
            if (event.task_id) {
              completeMultiWorker(
                assistantId,
                event.task_id,
                { summary: event.summary, durationMs: event.duration_ms },
                'ok',
              );
            }
            break;
          case 'agent.worker.error':
            if (event.task_id) {
              completeMultiWorker(
                assistantId,
                event.task_id,
                { message: event.message, durationMs: event.duration_ms },
                'error',
              );
            }
            break;
          case 'agent.worker.approval':
            // Une sous-tâche attend une validation humaine : le worker passe
            // en « awaiting_approval » (badge jaune dans la trace) et la carte
            // Approuver/Refuser est affichée. Le prompt de reprise est le
            // texte de la SOUS-TÂCHE (reprise ciblée, pas de l'orchestration).
            if (event.task_id && event.request_id) {
              completeMultiWorker(
                assistantId,
                event.task_id,
                { message: event.message, durationMs: event.duration_ms },
                'awaiting_approval',
              );
              setPendingApproval({
                requestId: event.request_id,
                prompt:
                  planTasks.find((task) => task.task_id === event.task_id)?.subtask ?? prompt,
                tool: event.approval?.tool ?? 'outil inconnu',
                reason: event.approval?.reason ?? 'validation humaine requise',
                args: event.approval?.args,
              });
            }
            break;
          case 'agent.done':
            // Orchestration interrompue sur une validation : le texte final
            // récapitulatif n'est affiché que si aucune carte n'est pendante
            // (la carte suffit comme signal pour l'utilisateur).
            if (event.answer && !event.answer.startsWith('Validation humaine requise')) {
              appendDelta(assistantId, event.answer ?? event.final_answer ?? '');
            }
            break;
          case 'agent.error':
            throw new Error(event.message || 'Echec de l orchestration multi-agents.');
          default:
            // agent.synthesizing et autres : rien a afficher pour l instant.
            break;
        }
      }
    },
    [
      appendDelta,
      completeMultiWorker,
      selectedModel,
      setMultiPlan,
      setPendingApproval,
      startMultiWorker,
    ],
  );

  const askAgentTurn = useCallback(
    async (
      assistantId: string,
      prompt: string,
      controller: AbortController,
      resumeRequestId?: string,
    ): Promise<void> => {
      /**
       * Applique le statut final du gate. ``alreadyStreamed`` vaut vrai quand
       * le texte a déjà été affiché via les deltas SSE (on ne le duplique pas).
       */
      const handleFinal = (data: AgentAskResponse, alreadyStreamed: boolean): void => {
        if (data.status === 'awaiting_approval' && data.request_id) {
          const tool = data.approval?.tool ?? 'outil inconnu';
          setPendingApproval({
            requestId: data.request_id,
            prompt,
            tool,
            reason: data.approval?.reason ?? 'validation humaine requise',
            args: data.approval?.args,
          });
          if (!alreadyStreamed) {
            appendDelta(assistantId, `[En attente de validation] L'action « ${tool} » nécessite votre décision avant exécution.`);
          }
          return;
        }
        // completed ou rejected : la réponse backend porte déjà l'explication.
        if (!alreadyStreamed) appendDelta(assistantId, data.response || '');
      };

      /** Repli historique : POST bloquant /api/agent/ask (réponse d'un bloc). */
      const askAgentBlocking = async (): Promise<void> => {
        // Contrat backend (schéma AskRequest) : champ en snake_case, comme
        // « enable_thinking » — la forme camelCase serait ignorée par Pydantic.
        const blockingBody: { prompt: string; session_id?: string; resume_request_id?: string } = { prompt };
        if (sessionId) blockingBody.session_id = sessionId;
        if (resumeRequestId) blockingBody.resume_request_id = resumeRequestId;

        const response = await fetch(AGENT_ASK_ENDPOINT, {
          method: 'POST',
          headers,
          body: JSON.stringify(blockingBody),
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(await apiErrorMessage(response));
        }
        const data = (await response.json()) as AgentAskResponse;
        handleFinal(data, false);
      };

      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      const apiKey = resolveApiKey();
      if (apiKey) headers['X-API-Key'] = apiKey;

      // Contrat backend (schéma AskStreamRequest) : champs snake_case ; les
      // sélecteurs de l'en-tête du chat (modèle, Réflexion) sont transmis.
      const body: Record<string, unknown> = { prompt };
      if (sessionId) body.session_id = sessionId;
      if (resumeRequestId) body.resume_request_id = resumeRequestId;
      if (selectedModel) body.model = selectedModel;
      if (enableThinking) body.enable_thinking = true;

      const response = await fetch(AGENT_ASK_STREAM_ENDPOINT, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      // Backend sans endpoint de streaming : repli transparent.
      if (!response.ok && (response.status === 404 || response.status === 405)) {
        await askAgentBlocking();
        return;
      }
      if (!response.ok) {
        throw new Error(await apiErrorMessage(response));
      }

      const contentType = response.headers.get('content-type') ?? '';
      if (!(contentType.includes('text/event-stream') && response.body)) {
        // Réponse JSON classique du gate (sans flux) : même rendu que le bloquant.
        const data = (await response.json()) as AgentAskResponse;
        handleFinal(data, false);
        return;
      }

      let streamedChars = 0;
      for await (const payload of readSseEvents(response.body)) {
        if (payload === '[DONE]') break;

        let event: ChatStreamEvent;
        try {
          event = JSON.parse(payload) as ChatStreamEvent;
        } catch {
          // Charge utile non JSON : affichée telle quelle (tolérance).
          appendDelta(assistantId, payload);
          streamedChars += payload.length;
          continue;
        }

        if (event.error) throw new Error(event.error);
        if (event.tool_start?.tool) {
          appendToolCall(assistantId, {
            tool: event.tool_start.tool,
            args: event.tool_start.args,
            status: 'running',
          });
        }
        if (event.tool_result?.tool) completeToolCall(assistantId, event.tool_result);
        if (event.thinking_delta) appendThinkingDelta(assistantId, event.thinking_delta);
        if (event.delta) {
          appendDelta(assistantId, event.delta);
          streamedChars += event.delta.length;
        }
        if (event.final) handleFinal(event.final, streamedChars > 0);
      }
    },
    [appendDelta, appendThinkingDelta, appendToolCall, completeToolCall, selectedModel, enableThinking, sessionId],
  );

  /** Envoie le message de l'utilisateur puis diffuse la réponse de l'IA en streaming. */
  const sendMessage = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || isLoading) return;

      // Historique exploitable par le backend (sans erreurs ni contenu vide).
      const history = messagesRef.current
        .filter((message) => !message.error && message.content.length > 0)
        .map((message) => ({ role: message.role, content: message.content }));

      const assistantId = createId();
      setMessages((previous) => [
        ...previous,
        { id: createId(), role: 'user', content: trimmed, createdAt: nowIso() },
        {
          id: assistantId,
          role: 'assistant',
          content: '',
          createdAt: nowIso(),
          streaming: true,
          thinking: '',
          thinkingStreaming: false,
        },
      ]);
      setStickToBottom(true);
      setIsLoading(true);

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        // Mode Agent : POST /api/agent/ask (réponse d'un bloc, gate
        // auto_approve/approve/reject) au lieu du chat SSE /api/ai.
        if (multiMode) {
          await askMultiAgentTurn(assistantId, trimmed, controller);
          return;
        }
        if (agentMode) {
          await askAgentTurn(assistantId, trimmed, controller);
          return;
        }

        const body: ChatRequestBody = { message: trimmed, history };
        // Conversation active (persistance serveur). Absent ou '' : le backend
        // crée une session à la volée (ou laisse l'échange hors journal).
        if (sessionId) body.session_id = sessionId;
        // Modèle choisi via le sélecteur de l'en-tête ('' = défaut serveur).
        if (selectedModel) body.model = selectedModel;
        // Mode « Réflexion » : la trace arrivera via les événements thinking_delta.
        // Le backend déclare le champ en snake_case (« enable_thinking ») : la
        // forme camelCase serait ignorée par Pydantic et le mode ne s'activerait pas.
        if (enableThinking) body.enable_thinking = true;
        // POST /api/ai est protégé par require_api_key côté backend : on
        // transmet la clé via X-API-Key quand elle est disponible.
        const headers: Record<string, string> = { 'Content-Type': 'application/json' };
        const apiKey = resolveApiKey();
        if (apiKey) headers['X-API-Key'] = apiKey;

        const response = await fetch(AI_ENDPOINT, {
          method: 'POST',
          headers,
          body: JSON.stringify(body),
          signal: controller.signal,
        });

        if (!response.ok) {
          throw new Error(await apiErrorMessage(response));
        }

        const contentType = response.headers.get('content-type') ?? '';

        if (contentType.includes('text/event-stream') && response.body) {
          // Mode streaming : chaque événement SSE enrichit la bulle au fil de l'eau.
          for await (const payload of readSseEvents(response.body)) {
            if (payload === '[DONE]') break;

            let event: ChatStreamEvent;
            try {
              event = JSON.parse(payload) as ChatStreamEvent;
            } catch {
              // Charge utile non JSON : affichée telle quelle (tolérance).
              appendDelta(assistantId, payload);
              continue;
            }

            if (event.error) throw new Error(event.error);
            if (event.thinking_delta) appendThinkingDelta(assistantId, event.thinking_delta);
            if (event.delta) appendDelta(assistantId, event.delta);
          }
        } else {
          // Repli : réponse JSON classique, non streamée.
          const data = (await response.json()) as { content?: string };
          appendDelta(assistantId, data.content ?? '');
        }
      } catch (error) {
        // Une annulation volontaire (bouton Stop) n'est pas une erreur.
        if (!controller.signal.aborted) {
          const detail = error instanceof Error ? error.message : String(error);
          patchMessage(assistantId, { error: detail });
        }
      } finally {
        patchMessage(assistantId, { streaming: false, thinkingStreaming: false });
        setIsLoading(false);
        abortRef.current = null;
      }
    },
    [isLoading, appendDelta, appendThinkingDelta, patchMessage, selectedModel, enableThinking, agentMode, askAgentTurn, sessionId],
  );

  /**
   * Décision humaine : APPROUVER une action en attente. La demande passe à
   * « approved » côté store, puis le run interrompu est relancé via
   * resume_request_id — l'action est exécutée UNE fois et l'agent conclut.
   */
  const handleApprove = useCallback(async () => {
    if (!pendingApproval || isLoading) return;
    const { requestId, prompt } = pendingApproval;
    setPendingApproval(null);
    setIsLoading(true);

    const assistantId = createId();
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const headers: Record<string, string> = {};
      const apiKey = resolveApiKey();
      if (apiKey) headers['X-API-Key'] = apiKey;
      const response = await fetch(`${APPROVALS_ENDPOINT}/${requestId}/approve`, {
        method: 'POST',
        headers,
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error(await apiErrorMessage(response));
      }

      // Reprise du run : nouvelle bulle, alimentée par la suite du run agent.
      setMessages((previous) => [
        ...previous,
        {
          id: assistantId,
          role: 'assistant',
          content: '',
          createdAt: nowIso(),
          streaming: true,
        },
      ]);
      await askAgentTurn(assistantId, prompt, controller, requestId);
    } catch (error) {
      if (!controller.signal.aborted) {
        patchMessage(assistantId, {
          error: error instanceof Error ? error.message : String(error),
          streaming: false,
        });
      }
    } finally {
      if (!controller.signal.aborted) {
        patchMessage(assistantId, { streaming: false });
      }
      setIsLoading(false);
      abortRef.current = null;
    }
  }, [pendingApproval, isLoading, askAgentTurn, patchMessage]);

  /**
   * Décision humaine : REFUSER une action en attente. Aucune exécution ; un
   * message explicite trace le refus dans la conversation.
   */
  const handleReject = useCallback(async () => {
    if (!pendingApproval || isLoading) return;
    const { requestId, tool, reason } = pendingApproval;
    setPendingApproval(null);
    setIsLoading(true);
    try {
      const headers: Record<string, string> = {};
      const apiKey = resolveApiKey();
      if (apiKey) headers['X-API-Key'] = apiKey;
      const response = await fetch(`${APPROVALS_ENDPOINT}/${requestId}/reject`, {
        method: 'POST',
        headers,
      });
      if (!response.ok) {
        throw new Error(await apiErrorMessage(response));
      }
      setMessages((previous) => [
        ...previous,
        {
          id: createId(),
          role: 'assistant' as const,
          content: `[Action refusée] « ${tool} » n'a pas été exécutée. Motif du contrôle : ${reason}.`,
          createdAt: nowIso(),
        },
      ]);
    } catch (error) {
      setMessages((previous) => [
        ...previous,
        {
          id: createId(),
          role: 'assistant' as const,
          content: '',
          createdAt: nowIso(),
          error: error instanceof Error ? error.message : String(error),
        },
      ]);
    } finally {
      setIsLoading(false);
    }
  }, [pendingApproval, isLoading]);

  const isEmpty = messages.length === 0;
  const canStartNewSession = !isEmpty || isLoading;
  const headerActions = (
    <>
      <SessionSelector
        sessions={sessions}
        selectedId={sessionId}
        onSelect={selectSession}
        isLoading={isLoading}
      />
      <button
        type="button"
        className="copilot-chat__think-toggle"
        data-active={multiMode || undefined}
        onClick={handleMultiToggle}
        aria-pressed={multiMode}
        title="Mode Multi-agents : un superviseur planifie, distribue des sous-taches a des agents spécialisés puis synthetise (trace temps reel)"
      >
        <TeamIcon />
        <span className="copilot-chat__think-label">Multi-agents</span>
      </button>
      <button
        type="button"
        className="copilot-chat__think-toggle"
        data-active={agentMode || undefined}
        onClick={handleAgentToggle}
        aria-pressed={agentMode}
        title="Mode Agent : l'agent peut exécuter ses outils ; une action à risque attend votre validation (approve / reject)"
      >
        <BotIcon />
        <span className="copilot-chat__think-label">Agent</span>
      </button>
      <button
        type="button"
        className="copilot-chat__think-toggle"
        data-active={enableThinking || undefined}
        onClick={handleThinkingToggle}
        aria-pressed={enableThinking}
        title="Mode Réflexion : l'agent raisonne avant de répondre (trace affichée)"
      >
        <ThinkIcon />
        <span className="copilot-chat__think-label">Réflexion</span>
      </button>
      <ChatModelSelector
        models={llmModels}
        selected={selectedModel}
        onChange={handleModelChange}
        loading={modelsLoading}
        error={modelsError}
      />
      {isLoading && (
        <span className="copilot-chat__spinner" role="status" aria-label="Génération en cours" />
      )}
      <button
        type="button"
        className="copilot-chat__new-task"
        onClick={startNewSession}
        disabled={!canStartNewSession}
        title="Nouvelle tâche (nouvelle session)"
        aria-label="Nouvelle tâche : démarrer une nouvelle session de chat"
      >
        <PlusIcon />
      </button>
    </>
  );


  return (
    <section className="copilot-chat" aria-label="Chat avec l'assistant IA">
      <header className="copilot-chat__header">
        <span className="copilot-chat__status-dot" data-active={isLoading} aria-hidden="true" />
        <h2 className="copilot-chat__title">Assistant IA</h2>
        <div className="copilot-chat__actions">
          {headerActions}
        </div>
      </header>

      <div
        className="copilot-chat__list"
        ref={listRef}
        onScroll={handleScroll}
        role="log"
        aria-live="polite"
      >
        {isEmpty ? (
          <div className="copilot-chat__empty">
            <p className="copilot-chat__empty-title">👋 Posez votre première question</p>
            <p className="copilot-chat__empty-hint">
              Les réponses sont générées en direct par votre backend <code>/api/ai</code>.
            </p>
          </div>
        ) : (
          messages.map((message) => <ChatMessage key={message.id} message={message} />)
        )}
      </div>

      {!stickToBottom && !isEmpty && (
        <button type="button" className="copilot-chat__jump" onClick={scrollToBottom}>
          ↓ Revenir en bas
        </button>
      )}

      {pendingApproval && (
        <div
          className="approval-card"
          role="alertdialog"
          aria-label="Validation d'action requise"
          ref={approvalRef}
          tabIndex={-1}
        >
          <div className="approval-card__header">
            <span className="approval-card__badge">Validation requise</span>
            <code className="approval-card__tool">{pendingApproval.tool}</code>
          </div>
          <p className="approval-card__reason">{pendingApproval.reason}</p>
          <pre className="approval-card__args">{formatArgsPreview(pendingApproval.args)}</pre>
          <div className="approval-card__actions">
            <button
              type="button"
              className="approval-card__button approval-card__button--approve"
              onClick={handleApprove}
              disabled={isLoading}
            >
              ✓ Approuver et exécuter
            </button>
            <button
              type="button"
              className="approval-card__button approval-card__button--reject"
              onClick={handleReject}
              disabled={isLoading}
            >
              ✕ Refuser
            </button>
          </div>
        </div>
      )}

      <ChatInput busy={isLoading} onSend={sendMessage} onStop={stopGeneration} />
    </section>
  );
}

/** Icône « ampoule » du bouton Réflexion (mode chain-of-thought). */
function ThinkIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 3a6 6 0 0 0-3.6 10.8c.6.5 1 1.2 1.1 2l.1.7h4.8l.1-.7c.1-.8.5-1.5 1.1-2A6 6 0 0 0 12 3Z" />
      <path d="M9.5 19.5h5" />
      <path d="M10.5 22h3" />
    </svg>
  );
}

/** Icône « + » du bouton Nouvelle tâche (nouvelle session de chat). */
function PlusIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 5v14" />
      <path d="M5 12h14" />
    </svg>
  );
}

/** Icône « équipe » du bouton Multi-agents (superviseur + workers). */
function TeamIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="9" cy="8" r="3" />
      <path d="M3.5 19c.8-3 3-4.5 5.5-4.5s4.7 1.5 5.5 4.5" />
      <circle cx="17.5" cy="9.5" r="2.3" />
      <path d="M15.5 14.6c2.4.2 4.4 1.6 5 4.4" />
    </svg>
  );
}

/** Icône « robot » du bouton Mode Agent (outils + validation humaine). */
function BotIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="4" y="8" width="16" height="12" rx="2" />
      <path d="M12 8V4" />
      <circle cx="12" cy="3" r="1" />
      <path d="M9 13h.01" />
      <path d="M15 13h.01" />
      <path d="M9.5 17h5" />
    </svg>
  );
}

