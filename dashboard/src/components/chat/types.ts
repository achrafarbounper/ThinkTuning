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
  /**
   * Trace de raisonnement de l'assistant (mode « Réflexion » activé),
   * accumulée progressivement pendant le streaming. Vide côté utilisateur
   * ou quand le mode est désactivé.
   */
  thinking?: string;
  /** Vrai tant que la trace de réflexion est encore en cours d'émission. */
  thinkingStreaming?: boolean;
}

/** Corps JSON attendu par le backend : POST /api/ai */
export interface ChatRequestBody {
  /** Dernier message de l'utilisateur. */
  message: string;
  /** Historique des tours précédents. */
  history: Array<{ role: Role; content: string }>;
  /**
   * Nom du modèle LLM à utiliser (sélecteur de l'en-tête du chat).
   * Absent ou vide : modèle par défaut de la configuration serveur.
   */
  model?: string;
  /**
   * Mode « Réflexion » : l'agent raisonne avant de répondre et la trace est
   * diffusée via les événements SSE `thinking_delta`. Désactivé par défaut.
   *
   * Nom bref du champ côté backend (contrat /api/ai) : Pydantic attend la forme
   * snake_case « enable_thinking » — la forme camelCase est ignorée silencieusement.
   */
  enable_thinking?: boolean;
}

/** Un modèle LLM disponible côté serveur (installé sur Ollama). */
export interface LlmModelInfo {
  /** Nom exact attendu par l'API Ollama (ex : « llama3.1:8b »). */
  name: string;
  /** Taille sur disque en octets, si connue. */
  size?: number | null;
  /** Date de dernière modification (ISO 8601), si connue. */
  modified_at?: string | null;
  /** Vrai s'il s'agit du modèle par défaut de la configuration serveur. */
  is_default?: boolean;
}

/** Réponse de GET /api/models (liste des modèles + modèle actif). */
export interface LlmModelsResponse {
  /** Modèle par défaut côté serveur (variables AGENT_*). */
  active: string;
  /** Modèles installés, triés par nom. */
  models: LlmModelInfo[];
}

/** Événement SSE émis par le backend pendant la génération. */
export interface ChatStreamEvent {
  /** Fragment de texte à ajouter à la réponse. */
  delta?: string;
  /**
   * Fragment de la trace de réflexion (mode « Réflexion » activé). Ces
   * événements sont émis par le backend AVANT les fragments de réponse.
   */
  thinking_delta?: string;
  /** Message d'erreur éventuel envoyé par le backend. */
  error?: string;
}

/* --- Mode Agent : gate auto_approve / approve / reject --------------------- */

/**
 * Décision structurée (JSON horodaté) renvoyée par le gate côté backend
 * (ia/agent/approvals.py — PolicyDecision.to_dict()).
 */
export interface AgentApprovalInfo {
  /** Outil que l'agent souhaitait appeler. */
  tool: string;
  /** Arguments (tronqués) de l'appel. */
  args?: Record<string, unknown>;
  /** Décision brute : « auto_approve » | « approve » | « reject ». */
  decision?: string;
  /** Catégorie d'action (« read », « write », « exec »…). */
  category?: string;
  /** Motif lisible de la décision. */
  reason?: string;
  /** Empreinte SHA-256 des arguments canoniques (traçabilité). */
  args_hash?: string;
  /** Horodatage ISO 8601 UTC de la décision. */
  timestamp?: string;
}

/**
 * Réponse JSON de POST /api/agent/ask. En mode Agent (contrairement au SSE
 * /api/ai), la réponse arrive d'un bloc :
 *   - completed         : réponse finale dans `response` ;
 *   - awaiting_approval : une action attend validation (`request_id`) ;
 *   - rejected          : action bloquée par la policy (motif dans `response`).
 */
export interface AgentAskResponse {
  response: string;
  model: string;
  status: string;
  request_id?: string | null;
  approval?: AgentApprovalInfo | null;
}

/** Action en attente de décision humaine (carte Approuver / Refuser). */
export interface PendingApprovalData {
  /** Identifiant de la demande (POST /api/agent/approvals/{id}/…). */
  requestId: string;
  /** Prompt d'origine : requis pour relancer avec `resume_request_id`. */
  prompt: string;
  /** Outil concerné (affiché sur la carte). */
  tool: string;
  /** Motif de validation exigé. */
  reason: string;
  /** Arguments tronqués de l'appel (aperçu sur la carte). */
  args?: Record<string, unknown>;
}
