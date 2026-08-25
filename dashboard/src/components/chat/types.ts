/**
 * Types partagés par les composants du chat.
 */

/** Rôle d'un participant à la conversation. */
export type Role = 'user' | 'assistant';

/** Un message affiché dans la fenêtre de chat. */
export interface ChatMessageData {
  /** Identifiant unique du message. */
  id: string;
  /** Auteur du message. */
  role: Role;
  /** Contenu texte (accumulé progressivement pendant le streaming). */
  content: string;
  /** Date de création au format ISO 8601. */
  createdAt: string;
  /** Vrai tant que la réponse est encore en cours de génération. */
  streaming?: boolean;
  /** Message d'erreur si la génération a échoué. */
  error?: string;
}

/** Corps JSON attendu par le backend : POST /api/ai */
export interface ChatRequestBody {
  /** Dernier message de l'utilisateur. */
  message: string;
  /** Historique des tours précédents. */
  history: Array<{ role: Role; content: string }>;
}

/** Événement SSE émis par le backend pendant la génération. */
export interface ChatStreamEvent {
  /** Fragment de texte à ajouter à la réponse. */
  delta?: string;
  /** Message d'erreur éventuel envoyé par le backend. */
  error?: string;
}
