import { memo } from 'react';
import { ThinkingBlock } from './ThinkingBlock';
import { ToolCallBlock } from './ToolCallBlock';
import { MultiAgentTrace } from './MultiAgentTrace';
import { MarkdownContent } from './markdown';
import type { ChatMessageData } from './types';

interface ChatMessageProps {
  /** Message à afficher. */
  message: ChatMessageData;
}

const timeFormatter = new Intl.DateTimeFormat('fr-FR', {
  hour: '2-digit',
  minute: '2-digit',
});

function BotAvatar() {
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
      <rect x="4" y="8" width="16" height="11" rx="3" />
      <path d="M12 8V5" />
      <circle cx="12" cy="3.5" r="1.5" />
      <path d="M9 13h.01" strokeWidth={2.4} />
      <path d="M15 13h.01" strokeWidth={2.4} />
      <path d="M9.5 16.5h5" />
    </svg>
  );
}

function UserAvatar() {
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
      <circle cx="12" cy="8.5" r="3.5" />
      <path d="M5 20c1.5-3.5 4-5 7-5s5.5 1.5 7 5" />
    </svg>
  );
}

/** Trois points animés affichés avant l'arrivée du premier token. */
function TypingIndicator() {
  return (
    <span className="typing-dots" role="status" aria-label="L'assistant est en train d'écrire">
      <span />
      <span />
      <span />
    </span>
  );
}

/**
 * Bulle d'un message unique (utilisateur ou IA), façon GitHub Copilot Chat.
 *
 * Mémoïsée : pendant le streaming, seuls le message actif (et ses blocs
 * d'outils / réflexion) se re-rendent à chaque token — l'historique et son
 * rendu Markdown ne sont pas recalculés.
 */
export const ChatMessage = memo(function ChatMessage({ message }: ChatMessageProps) {
  const isUser = message.role === 'user';
  const classes = ['chat-message', `chat-message--${message.role}`];
  if (message.error) {
    classes.push('chat-message--error');
  }

  return (
    <article className={classes.join(' ')}>
      <div className="chat-message__avatar" aria-hidden="true">
        {isUser ? <UserAvatar /> : <BotAvatar />}
      </div>

      <div className="chat-message__body">
        {/* Chaîne d'outils appelés par l'agent (mode Agent streaming). */}
        {!isUser && message.toolCalls && message.toolCalls.length > 0 && (
          <ToolCallBlock calls={message.toolCalls} />
        )}

        {/* Orchestration multi-agents : plan + etat des workers (temps reel). */}
        {!isUser && (message.multiPlan || message.multiWorkers) && (
          <MultiAgentTrace plan={message.multiPlan} workers={message.multiWorkers} />
        )}

        {/* Trace de raisonnement (« Réflexion »), au-dessus de la bulle. */}
        {!isUser && message.thinking !== undefined && (
          <ThinkingBlock
            thinking={message.thinking}
            streaming={Boolean(message.thinkingStreaming)}
          />
        )}

        <div className="chat-message__bubble">
          {message.content ? (
            isUser ? (
              /* Utilisateur : texte brut fidèle, sauts de ligne préservés. */
              <p className="chat-message__text">{message.content}</p>
            ) : (
              /* Assistant : Markdown rendu (markdown.tsx) ; le curseur de
                 streaming est injecté en fin de dernier bloc. */
              <div className="chat-message__text chat-message__text--md">
                <MarkdownContent content={message.content} cursor={Boolean(message.streaming)} />
              </div>
            )
          ) : message.streaming ? (
            <TypingIndicator />
          ) : null}

          {message.error && <p className="chat-message__error">⚠ {message.error}</p>}
        </div>

        <span className="chat-message__meta">
          {isUser ? 'Vous' : 'Assistant'} · {timeFormatter.format(new Date(message.createdAt))}
        </span>
      </div>
    </article>
  );
});
