/**
 * Fenêtre de chat complète, façon GitHub Copilot Chat.
 *
 * Gère :
 * - l'historique des messages (bulles utilisateur / IA),
 * - trois routages de conversation (mutuellement exclusifs) :
 *     · Chat (défaut) : streaming POST /api/v1/chat/ai,
 *     · Agent (v2)    : noyau agentique POST /api/v1/agent/ask/core — boucle
 *       Intent -> Plan -> Policy -> Budget -> Action, appels d'outils
 *       streamés (core_tool) et carte de validation humaine,
 *     · Multi-agents  : orchestration superviseur / workers via
 *       POST /api/v1/agent/multi/ask/stream (SSE nommé agent.*),
 * - l'authentification : session JWT (Authorization: Bearer) prioritaire,
 *   repli X-API-Key (config dashboard ou VITE_API_KEY),
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
import type {
  ChatMessageData,
  ChatSessionInfo,
  LlmModelInfo,
  LlmModelsResponse,
  PendingApprovalData,
  StoredMessage,
  ToolCallData,
  ToolCallStatus,
} from './types';
import './chat.css';
import { useApp } from "../../context/useApp";
import {
  apiErrorMessage,
  createId,
  nowIso,
  resolveApiKey,
  resolveAuthHeaders,
  resolveBaseUrl,
  MODELS_ENDPOINT,
  SESSIONS_ENDPOINT,
} from './chatTransport';
import { useAssistantTurns } from './useAssistantTurns';
import { useChatApprovals } from './useChatApprovals';
import { useMultiAgentTrace } from './useMultiAgentTrace';
import { McpStatusBadge } from './McpStatusBadge';

/** Clé de persistance du modèle LLM choisi pour le chat (localStorage). */
const CHAT_MODEL_STORAGE_KEY = 'thinktuning.chatModel';

/** Clé de persistance de la conversation active (localStorage). */
const CHAT_SESSION_STORAGE_KEY = 'thinktuning.chatSession';

/** Clé de persistance du mode « Réflexion » (localStorage). */
const THINKING_STORAGE_KEY = 'thinktuning.enableThinking';

/**
 * Cle de persistance du mode « Multi-agents » (orchestration superviseur /
 * workers via /api/v1/agent/multi/ask/stream). Mutuellement exclusif avec le
 * mode Agent (noyau v2).
 */
const MULTI_MODE_STORAGE_KEY = 'thinktuning.multiAgentMode';

/**
 * Clé de persistance du mode « Agent (v2) » — boucle agentique
 * Intent -> Plan -> Policy -> Budget -> Action via /api/v1/agent/ask/core.
 * Mutuellement exclusif avec le mode Multi-agents.
 */
const CORE_MODE_STORAGE_KEY = 'thinktuning.coreMode';

/**
 * Clé de persistance du mode « MCP » (S7 — tâche 20) : les tours d'assistant
 * partent vers la surface MCP (POST /mcp/sse, tool `orchestrate`) au lieu de
 * l'API HTTP legacy — lue seule quand le flag backend MCP_FIRST est actif.
 * Mutuellement exclusif avec les modes Multi-agents et Agent (v2).
 */
const MCP_MODE_STORAGE_KEY = 'thinktuning.mcpMode';

/** Clé de persistance du sous-mode d'orchestration utilisé par MCP. */
const MCP_AGENT_MODE_STORAGE_KEY = 'thinktuning.mcpAgentMode';

/** Nombre maximal de caractères d'arguments affichés sur la carte d'approbation. */
const APPROVAL_ARGS_PREVIEW_LIMIT = 400;

/** Distance (px) sous laquelle on considère que l'utilisateur « suit » le bas. */
const SCROLL_THRESHOLD_PX = 80;

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

/** Relit l etat persiste du mode « Multi-agents » (desactive par defaut). */
function loadStoredMultiMode(): boolean {
  try {
    return window.localStorage.getItem(MULTI_MODE_STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

/** Relit l'état persisté du mode « Noyau v2 » (désactivé par défaut). */
function loadStoredCoreMode(): boolean {
  try {
    return window.localStorage.getItem(CORE_MODE_STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

/** Relit l'état persisté du mode « MCP » (désactivé par défaut). */
function loadStoredMcpMode(): boolean {
  try {
    return window.localStorage.getItem(MCP_MODE_STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

/**
 * Modes de conversation mutuellement exclusifs : { mcp, core, multi }.
 *
 * L'exclusivité n'est garantie qu'à l'usage (chaque bascule désactive les
 * autres), mais des clés localStorage historiques peuvent porter PLUSIEURS
 * flags actifs simultanément. La priorité retenue est MCP > Agent (v2) >
 * Multi-agents ; `normalized` signale qu'une correction a eu lieu (à
 * re-persister).
 */
interface ExclusiveModes {
  mcp: boolean;
  core: boolean;
  multi: boolean;
  normalized: boolean;
}

/**
 * Résout les trois flags de mode — appelé UNE fois, dans un initialiseur
 * paresseux de `useState` : le premier rendu est déjà cohérent, sans
 * `setState` synchrone dans un effet (rendu en cascade évité).
 */
function resolveExclusiveModes(): ExclusiveModes {
  const mcp = loadStoredMcpMode();
  const core = loadStoredCoreMode();
  const multi = loadStoredMultiMode();
  if ([mcp, core, multi].filter(Boolean).length <= 1) {
    return { mcp, core, multi, normalized: false };
  }
  return { mcp, core: !mcp && core, multi: !mcp && !core && multi, normalized: true };
}

/** Relit le sous-mode MCP choisi dans l'IHM (multi-agent par défaut). */
function loadStoredMcpAgentMode(): 'mono_agent' | 'multi_agent' {
  try {
    return window.localStorage.getItem(MCP_AGENT_MODE_STORAGE_KEY) === 'mono_agent'
      ? 'mono_agent'
      : 'multi_agent';
  } catch {
    return 'multi_agent';
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
        // Args stockés en string par l'ancien agent, en objet par le noyau v2.
        args:
          typeof event.args === 'string'
            ? event.args
            : event.args != null
              ? JSON.stringify(event.args)
              : undefined,
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


export function ChatWindow() {
  // Clé API saisie dans les Settings (mémoire de session — AppProvider) :
  // prioritaire pour le transport MCP (qui exige X-API-Key, jamais Bearer).
  const { config } = useApp();
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
  // Mode « Multi-agents » : les messages partent vers /api/agent/multi/ask/
  // stream (superviseur : plan -> dispatch -> synthese) et la trace temps reel
  // (plan + workers) est affichee dans la bulle de reponse.
  // Exclusivité des modes résolue AU MONTAGE (initialiseur paresseux) : le
  // premier rendu est déjà cohérent — aucun setState dans un effet.
  const [initialModes] = useState<ExclusiveModes>(resolveExclusiveModes);
  const [multiMode, setMultiMode] = useState<boolean>(initialModes.multi);
  // Mode « Agent (v2) » : les messages partent vers /api/agent/ask/core
  // (boucle Intent -> Plan -> Policy -> Budget -> Action ; le noyau est actif
  // par défaut côté backend, une réponse 503 signale un repli legacy volontaire).
  const [coreMode, setCoreMode] = useState<boolean>(initialModes.core);
  // Mode « MCP » (S7 — tâche 20) : les tours partent vers la surface MCP
  // (POST /mcp/sse, tool `orchestrate`) au lieu de l'API HTTP legacy —
  // le canal à privilégier quand MCP_FIRST=true gèle l'HTTP en lecture seule.
  const [mcpMode, setMcpMode] = useState<boolean>(initialModes.mcp);
  // Sous-mode MCP choisi dans le chat : mono-agent ou multi-agent.
  const [mcpAgentMode, setMcpAgentMode] = useState<'mono_agent' | 'multi_agent'>(
    loadStoredMcpAgentMode,
  );
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
        // Route protégée côté backend : session JWT prioritaire, repli
        // X-API-Key (même contrat que le transport clientCore).
        const headers: Record<string, string> = resolveAuthHeaders();

        const base = resolveBaseUrl();
        const response = await fetch(`${base}${MODELS_ENDPOINT}`, { headers });
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
      // Session JWT prioritaire, repli X-API-Key (cf. resolveAuthHeaders).
      const headers: Record<string, string> = resolveAuthHeaders();
      try {
        const base = resolveBaseUrl();
        const response = await fetch(`${base}${SESSIONS_ENDPOINT}`, { headers });
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

  // Persiste l'exclusivité des modes résolue au montage (cf. initialModes).
  // Les flags en mémoire sont déjà exclusifs (priorité MCP > Agent v2 >
  // Multi-agents) : cet effet ne fait QUE réécrire localStorage quand une
  // correction a été nécessaire — aucun setState, donc aucun rendu en
  // cascade (le correctif précédent rappelait trois setState au montage).
  useEffect(() => {
    if (!initialModes.normalized) return;
    try {
      window.localStorage.setItem(MCP_MODE_STORAGE_KEY, String(initialModes.mcp));
      window.localStorage.setItem(CORE_MODE_STORAGE_KEY, String(initialModes.core));
      window.localStorage.setItem(MULTI_MODE_STORAGE_KEY, String(initialModes.multi));
    } catch {
      /* stockage indisponible : l'état en mémoire reste cohérent */
    }
  }, [initialModes]);

  /** Charge les messages d'une conversation existante et la rend active. */
  const selectSession = useCallback(
    async (id: string): Promise<void> => {
      abortRef.current?.abort();
      // Session JWT prioritaire, repli X-API-Key (cf. resolveAuthHeaders).
      const headers: Record<string, string> = resolveAuthHeaders();
      try {
        const base = resolveBaseUrl();
        const response = await fetch(`${base}${SESSIONS_ENDPOINT}/${id}/messages`, { headers });
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
              // Trace de réflexion PERSISTÉE (mode « Réflexion »). Le code
              // initial recopiait le CONTENU de la réponse dans thinking,
              // dupliquant la réponse dans un bloc « Réflexion » au
              // rechargement ; seules les traces réellement journalisées
              // sont désormais affichées (absentes des sessions anciennes).
              thinking:
                message.role === 'assistant' && message.thinking
                  ? message.thinking
                  : undefined,
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
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...resolveAuthHeaders(),
    };
    try {
      const base = resolveBaseUrl();
      const response = await fetch(`${base}${SESSIONS_ENDPOINT}`, {
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
   * Bascule le mode « Multi-agents » (et désactive le mode Agent : les deux
   * routages de message sont mutuellement exclusifs).
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
      setCoreMode(false);
      try {
        window.localStorage.setItem(CORE_MODE_STORAGE_KEY, 'false');
      } catch {
        /* idem */
      }
      setMcpMode(false);
      try {
        window.localStorage.setItem(MCP_MODE_STORAGE_KEY, 'false');
      } catch {
        /* idem */
      }
    }
  }, [multiMode]);

  /**
   * Active/désactive le mode « Agent (v2) » — routage vers le noyau agentique
   * (/ask/core) — et persiste le choix. Quitter le mode annule la carte
   * d'approbation affichée (le backend reste maître de la demande, qui expire
   * seule côté store). Mutuellement exclusif avec le mode Multi-agents.
   */
  const handleCoreToggle = useCallback(() => {
    setCoreMode((previous) => {
      const next = !previous;
      try {
        window.localStorage.setItem(CORE_MODE_STORAGE_KEY, String(next));
      } catch {
        /* stockage indisponible : le choix reste valable pour la session */
      }
      return next;
    });
    setPendingApproval(null);
    setMultiMode(false);
    try {
      window.localStorage.setItem(MULTI_MODE_STORAGE_KEY, 'false');
    } catch {
      /* idem */
    }
    setMcpMode(false);
    try {
      window.localStorage.setItem(MCP_MODE_STORAGE_KEY, 'false');
    } catch {
      /* idem */
    }
  }, []);

  /**
   * Bascule le mode « MCP » (S7 — tâche 20) : les tours d'assistant partent
   * vers la surface MCP (transport POST /mcp/sse, tool `orchestrate`) — le
   * canal privilégié quand MCP_FIRST=true gèle l'API HTTP legacy. Désactive
   * les modes Multi-agents et Agent (v2) : les trois routages de messages
   * restent mutuellement exclusifs.
   */
  const handleMcpToggle = useCallback(() => {
    setMcpMode((previous) => {
      const next = !previous;
      try {
        window.localStorage.setItem(MCP_MODE_STORAGE_KEY, String(next));
      } catch {
        /* stockage indisponible : le choix reste valable pour la session */
      }
      return next;
    });
    setPendingApproval(null);
    setMultiMode(false);
    try {
      window.localStorage.setItem(MULTI_MODE_STORAGE_KEY, 'false');
    } catch {
      /* idem */
    }
    setCoreMode(false);
    try {
      window.localStorage.setItem(CORE_MODE_STORAGE_KEY, 'false');
    } catch {
      /* idem */
    }
  }, []);

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

  // --- L3 (SCRUM-154) : moteur de tours + approbations + trace partagée ------
  // Miroir du flag « occupé » pour les callbacks asynchrones (jamais muté
  // pendant le rendu — cf. règle react-hooks/refs).
  const isLoadingRef = useRef(isLoading);
  useEffect(() => {
    isLoadingRef.current = isLoading;
  });

  // Trace multi-agent PARTAGÉE (useReducer + localStorage) : alimentée par
  // les événements MCP (union discriminée McpOrchestrateEvent), restaurée au
  // montage (elle survit au rechargement de la page) et réinitialisée à
  // chaque NOUVEAU tour MCP (reset appelé par useAssistantTurns — jamais
  // sur une erreur réseau : la trace accumulée reste visible).
  const { trace, handleMcpEvent, reset: resetTrace } = useMultiAgentTrace();

  // Moteur des tours : route MCP > Multi-agents > Agent (v2) > Chat (défaut).
  const turns = useAssistantTurns({
    sessionApiKey: config.apiKey ?? '',
    sessionId,
    selectedModel,
    enableThinking,
    mcpMode,
    multiMode,
    coreMode,
    mcpAgentMode,
    isLoadingRef,
    messagesRef,
    setMessages,
    setIsLoading,
    abortRef,
    setPendingApproval,
    onTraceEvent: handleMcpEvent,
    onTraceReset: resetTrace,
  });
  const {
    sendMessage,
    stopGeneration,
    askMcpTurn,
    askMultiAgentTurn,
    askCoreTurn,
    patchMessage,
    flushStreamBuffer,
    activeAssistantIdRef,
  } = turns;

  // Miroir de la trace dans la bulle assistant ACTIVE (rendu MultiAgentTrace
  // au-dessus de la réponse). Effet sur `trace` : une seule source d'écriture
  // (l'état partagé), la bulle ne fait que refléter — jamais l'inverse.
  useEffect(() => {
    const assistantId = activeAssistantIdRef.current;
    if (assistantId && Object.keys(trace).length > 0) {
      patchMessage(assistantId, { trace });
    }
  }, [trace, patchMessage, activeAssistantIdRef]);

  // Décisions humaines (carte Approuver / Refuser) — reprise du run dans le
  // canal d'origine (multi / mcp / core).
  const { handleApprove, handleReject } = useChatApprovals({
    pendingApproval,
    isLoading,
    setPendingApproval,
    setMessages,
    setIsLoading,
    abortRef,
    askMcpTurn,
    askMultiAgentTurn,
    askCoreTurn,
    patchMessage,
    flushStreamBuffer,
  });

  // Repli HTTP legacy (L3) : un tour MCP échoué (401/403/503/réseau) porte un
  // `fallbackPrompt` sur la bulle — le bouton ci-dessous rejoue la demande via
  // l'API legacy (noyau v2) SANS changer le mode courant ni vider la trace.
  const lastFallbackMessage = [...messages]
    .reverse()
    .find((message) => message.role === 'assistant' && message.fallbackPrompt);
  const resendViaHttp = useCallback(async () => {
    const prompt = lastFallbackMessage?.fallbackPrompt;
    if (!prompt || isLoading) return;
    // Nettoie d'abord le fallbackPrompt : un seul clic effectif (idempotent).
    patchMessage(lastFallbackMessage!.id, { fallbackPrompt: undefined });
    const assistantId = createId();
    activeAssistantIdRef.current = assistantId;
    // Pas de nouvelle bulle utilisateur : le prompt est déjà dans l'historique.
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
    setIsLoading(true);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      // Le tool MCP `orchestrate` encapsule le noyau v2 : le repli fidèle
      // passe par le même pipeline en HTTP (POST /api/v1/agent/ask/core).
      await askCoreTurn(assistantId, prompt, controller);
    } catch (error) {
      if (!controller.signal.aborted) {
        patchMessage(assistantId, {
          error: error instanceof Error ? error.message : String(error),
        });
      }
    } finally {
      flushStreamBuffer();
      patchMessage(assistantId, { streaming: false });
      setIsLoading(false);
      abortRef.current = null;
    }
  }, [
    abortRef,
    activeAssistantIdRef,
    askCoreTurn,
    flushStreamBuffer,
    isLoading,
    lastFallbackMessage,
    patchMessage,
    setIsLoading,
    setMessages,
  ]);


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
      {/* L3 (SCRUM-154) : diagnostic MCP (initialize → ping → tools/list) ;
          preflight au montage du badge (donc à l'activation du mode MCP),
          re-diagnostic au clic. Visible uniquement en mode MCP. */}
      {mcpMode && (
        <McpStatusBadge
          baseUrl={resolveBaseUrl()}
          apiKey={config.apiKey ?? resolveApiKey()}
          autoCheck
        />
      )}
      <button
        type="button"
        className="copilot-chat__think-toggle"
        data-active={mcpMode || undefined}
        onClick={handleMcpToggle}
        aria-pressed={mcpMode}
        title="Mode MCP (S7) : les tours partent vers la surface MCP (POST /mcp/sse, tool orchestrate) au lieu de l'API HTTP legacy. À privilégier quand MCP_FIRST=true gèle l'HTTP en lecture seule."
      >
        <McpIcon />
        <span className="copilot-chat__think-label">MCP</span>
      </button>
      <label className="copilot-chat__mcp-mode">
        <span className="copilot-chat__mcp-mode-label">Orchestration MCP</span>
        <select
          aria-label="Mode d'orchestration MCP"
          value={mcpAgentMode}
          disabled={!mcpMode || isLoading}
          onChange={(event) => {
            const next = event.target.value === 'mono_agent' ? 'mono_agent' : 'multi_agent';
            setMcpAgentMode(next);
            try {
              window.localStorage.setItem(MCP_AGENT_MODE_STORAGE_KEY, next);
            } catch {
              /* le choix reste actif pour la session */
            }
          }}
        >
          <option value="multi_agent">Multi-agent</option>
          <option value="mono_agent">Mono-agent</option>
        </select>
      </label>
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
        data-active={coreMode || undefined}
        onClick={handleCoreToggle}
        aria-pressed={coreMode}
        title="Mode Agent (Noyau v2) : boucle Intent -> Plan -> Policy -> Budget -> Action ; exécution réelle des outils, sandbox policy (AUTO_APPROVE / APPROVE / REJECT), budget plafonné. Une action à risque attend votre validation (approve / reject). Actif par défaut côté backend (AGENT_NEW_CORE)."
      >
        <BotIcon />
        <span className="copilot-chat__think-label">Agent (v2)</span>
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
              Les réponses sont générées en direct par votre backend <code>/api/v1/chat/ai</code>.
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

      {/* L3 (SCRUM-154) : repli HTTP legacy — proposé quand un tour MCP a
          échoué avec une erreur où l'API HTTP reste une option. La trace
          accumulée n'est PAS effacée ; le prompt est rejoué tel quel. */}
      {lastFallbackMessage && !isLoading && (
        <div className="copilot-chat__fallback" role="status">
          <p className="copilot-chat__fallback-hint">
            Le transport MCP a échoué. Renvoyez cette demande via l'API HTTP legacy.
          </p>
          <button
            type="button"
            className="copilot-chat__fallback-button"
            onClick={() => {
              void resendViaHttp();
            }}
          >
            ⇄ Renvoyer via HTTP
          </button>
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

/** Icône « robot » du bouton Mode Agent (noyau v2 : outils + validation humaine). */
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

/** Icône « MCP » du bouton Mode MCP (transport POST /mcp/sse — câble/prise). */
function McpIcon() {
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
      <rect x="3" y="10" width="18" height="10" rx="2" />
      <rect x="3" y="2" width="18" height="4" rx="1" />
      <path d="M5 4h14" />
      <path d="M7 14h3M14 14h3M7 17h3M14 17h3" />
    </svg>
  );
}
