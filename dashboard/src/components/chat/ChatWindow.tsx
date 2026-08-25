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
import { readSseEvents } from './streamSse';
import type {
  ChatMessageData,
  ChatRequestBody,
  ChatStreamEvent,
  LlmModelInfo,
  LlmModelsResponse,
} from './types';
import './chat.css';

/** Endpoint du backend (proxifié par Vite vers l'API FastAPI en développement). */
const AI_ENDPOINT = '/api/ai';

/** Endpoint listant les modèles LLM disponibles (même proxy que /api/ai). */
const MODELS_ENDPOINT = '/api/models';

/** Clé de stockage partagée avec le dashboard (voir CONFIG_STORAGE_KEY dans dashboard-demo.jsx). */
const API_CONFIG_STORAGE_KEY = 'thinktuning.apiConfig';

/** Clé de persistance du modèle LLM choisi pour le chat (localStorage). */
const CHAT_MODEL_STORAGE_KEY = 'thinktuning.chatModel';

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

export function ChatWindow() {
  const [messages, setMessages] = useState<ChatMessageData[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [stickToBottom, setStickToBottom] = useState(true);

  // Sélecteur de modèle LLM : liste fournie par GET /api/models, choix
  // persisté en localStorage pour survivre au rechargement de la page.
  const [llmModels, setLlmModels] = useState<LlmModelInfo[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>(loadStoredChatModel);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [modelsError, setModelsError] = useState('');

  const listRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController>(null);

  // Miroir de l'état pour lire un historique à jour dans les callbacks asynchrones.
  const messagesRef = useRef(messages);
  messagesRef.current = messages;

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
          throw new Error(`Le serveur a répondu ${response.status} (${response.statusText})`);
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

  /** Change le modèle LLM utilisé par les prochains messages du chat. */
  const handleModelChange = useCallback((modelName: string) => {
    setSelectedModel(modelName);
    try {
      window.localStorage.setItem(CHAT_MODEL_STORAGE_KEY, modelName);
    } catch {
      /* stockage indisponible : la sélection reste valable pour la session */
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

  /** Ajoute un fragment de texte au message en cours de streaming. */
  const appendDelta = useCallback((id: string, delta: string) => {
    setMessages((previous) =>
      previous.map((message) =>
        message.id === id ? { ...message, content: message.content + delta } : message,
      ),
    );
  }, []);

  /** Modifie certains champs d'un message (fin de streaming, erreur…). */
  const patchMessage = useCallback((id: string, patch: Partial<ChatMessageData>) => {
    setMessages((previous) =>
      previous.map((message) => (message.id === id ? { ...message, ...patch } : message)),
    );
  }, []);

  /** Interrompt proprement la génération en cours. */
  const stopGeneration = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  /**
   * Démarre une nouvelle session (« Nouvelle tâche ») : interrompt la
   * génération éventuellement en cours puis vide la conversation. Le backend
   * /api/ai étant stateless (historique renvoyé à chaque requête), la
   * réinitialisation de l'état local suffit ; le nettoyage final du flux
   * interrompu est géré par le bloc finally de sendMessage().
   */
  const startNewSession = useCallback(() => {
    abortRef.current?.abort();
    setMessages([]);
    setStickToBottom(true);
  }, []);

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
        { id: assistantId, role: 'assistant', content: '', createdAt: nowIso(), streaming: true },
      ]);
      setStickToBottom(true);
      setIsLoading(true);

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        const body: ChatRequestBody = { message: trimmed, history };
        // Modèle choisi via le sélecteur de l'en-tête ('' = défaut serveur).
        if (selectedModel) body.model = selectedModel;
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
          throw new Error(`Le serveur a répondu ${response.status} (${response.statusText})`);
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
        patchMessage(assistantId, { streaming: false });
        setIsLoading(false);
        abortRef.current = null;
      }
    },
    [isLoading, appendDelta, patchMessage, selectedModel],
  );

  const isEmpty = messages.length === 0;
  /** Une nouvelle session n'a de sens que s'il y a quelque chose à réinitialiser. */
  const canStartNewSession = !isEmpty || isLoading;

  return (
    <section className="copilot-chat" aria-label="Chat avec l'assistant IA">
      <header className="copilot-chat__header">
        <span className="copilot-chat__status-dot" data-active={isLoading} aria-hidden="true" />
        <h2 className="copilot-chat__title">Assistant IA</h2>
        <div className="copilot-chat__actions">
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

      <ChatInput busy={isLoading} onSend={sendMessage} onStop={stopGeneration} />
    </section>
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

