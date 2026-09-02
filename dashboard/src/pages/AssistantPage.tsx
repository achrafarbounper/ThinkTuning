/**
 * Page « Assistant IA » — enveloppe la fenêtre de chat existante
 * (streaming SSE via POST /api/ai proxifié par Vite).
 */

import { ChatWindow } from "../components/chat";

export default function AssistantPage() {
  return (
    <>
      <header className="page-head">
        <h1>Assistant IA</h1>
        <p>
          Conversation en direct avec le backend (/api/ai) — réponses streamées,
          sélection du modèle LLM, sessions réinitialisables.
        </p>
      </header>
      <ChatWindow />
    </>
  );
}
