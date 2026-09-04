/**
 * Page « Assistant IA » — enveloppe la fenêtre de chat existante.
 *
 * Trois routages de conversation (mutuellement exclusifs) :
 *  - Chat (défaut)  : streaming SSE via POST /api/ai ;
 *  - Agent (v2)     : noyau agentique /api/agent/ask/core — boucle
 *    Intent -> Plan -> Policy -> Budget -> Action, exécution réelle d'outils,
 *    policy sandbox (AUTO_APPROVE / APPROVE / REJECT), budget plafonné et
 *    validation humaine des actions à risque (carte Approuver / Refuser,
 *    reprise via resume_request_id) ;
 *  - Multi-agents   : orchestration superviseur / workers
 *    (/api/agent/multi/ask/stream) avec trace temps réel dans la bulle.
 *
 * Les runs du noyau v2 sont persistés comme sessions de flux (événements
 * core.*) et rejouables dans la page Flow Map (GET /api/agent/flow).
 */

import { ChatWindow } from "../components/chat";

export default function AssistantPage() {
  return (
    <>
      <header className="page-head">
        <h1>Assistant IA</h1>
        <p>
          Conversation en direct avec le backend — réponses streamées, sélection
          du modèle LLM, sessions réinitialisables. Le <strong>mode Agent (v2)</strong>{" "}
          passe par le noyau agentique (Intent → Plan → Policy → Budget →
          Action) : outils réels, sandbox policy et validation humaine des
          actions à risque. Le <strong>mode Multi-agents</strong> planifie puis
          dispatche des sous-tâches à des workers spécialisés avant synthèse.
        </p>
      </header>
      <ChatWindow />
    </>
  );
}
