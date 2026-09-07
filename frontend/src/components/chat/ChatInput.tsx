import { useRef, useState } from 'react';
import type { FormEvent, KeyboardEvent } from 'react';

/** Hauteur maximale de la zone de saisie avant apparition du défilement interne. */
const MAX_TEXTAREA_HEIGHT = 160;

interface ChatInputProps {
  /** Vrai pendant la génération : l'envoi est bloqué et un bouton Stop apparaît. */
  busy: boolean;
  /** Appelé avec le texte saisi lorsque l'utilisateur envoie un message. */
  onSend: (text: string) => void;
  /** Appelé pour interrompre la génération en cours. */
  onStop: () => void;
}

/** Zone de saisie basse : textarea auto-extensible + bouton envoyer / stop. */
export function ChatInput({ busy, onSend, onStop }: ChatInputProps) {
  const [value, setValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const canSend = value.trim().length > 0 && !busy;

  /** Ajuste la hauteur du textarea à son contenu, dans la limite autorisée. */
  const resize = () => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = 'auto';
    element.style.height = `${Math.min(element.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
  };

  const submit = () => {
    const text = value.trim();
    if (!text || busy) return;
    onSend(text);
    setValue('');
    requestAnimationFrame(resize);
    textareaRef.current?.focus();
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    submit();
  };

  /** Entrée = envoyer, Maj+Entrée = retour à la ligne. */
  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <form className="chat-input" onSubmit={handleSubmit}>
      <textarea
        ref={textareaRef}
        className="chat-input__field"
        rows={1}
        value={value}
        placeholder={busy ? "L'assistant rédige une réponse…" : 'Posez votre question…'}
        onChange={(event) => {
          setValue(event.target.value);
          requestAnimationFrame(resize);
        }}
        onKeyDown={handleKeyDown}
        aria-label="Votre message"
      />

      {busy ? (
        <button
          type="button"
          className="chat-input__button chat-input__button--stop"
          onClick={onStop}
          title="Arrêter la génération"
          aria-label="Arrêter la génération"
        >
          <StopIcon />
        </button>
      ) : (
        <button
          type="submit"
          className="chat-input__button"
          disabled={!canSend}
          title="Envoyer (Entrée)"
          aria-label="Envoyer"
        >
          <SendIcon />
        </button>
      )}
    </form>
  );
}

function SendIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.9}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M3.4 11.05 20.2 3.9a.55.55 0 0 1 .72.72l-7.15 16.8a.55.55 0 0 1-1.02-.02l-2.53-6.17a.9.9 0 0 0-.49-.49l-6.17-2.53a.55.55 0 0 1-.02-1.02Z" />
      <path d="m10.3 13.7 3.6-3.6" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" />
    </svg>
  );
}
