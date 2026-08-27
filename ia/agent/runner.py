class AgentRunner:
    """Fine enveloppe autour d'AgentCore consommée par core.agent_cache.

    ``ask`` conserve le comportement historique (réponse finale seule, str).
    ``ask_detailed`` expose en plus la trace de réflexion collectée quand le
    mode « Réflexion » est activé (AgentCore(enable_thinking=True)).
    """

    def __init__(self, agent):
        self.agent = agent

    def ask(self, prompt: str):
        return self.agent.run(prompt)

    def ask_detailed(self, prompt: str, resume_request_id: str | None = None, history_messages=None):
        """Exécute le run complet et renvoie AgentResult(answer, thinking).

        ``resume_request_id`` relance une tâche en attente de validation après
        qu'une demande ``approve`` a été approuvée (voir AgentCore.run_detailed).
        ``history_messages`` (optionnel) : messages de conversation rejoués en
        tête du contexte LLM pour une mémoire de session.
        """
        return self.agent.run_detailed(
            prompt,
            resume_request_id=resume_request_id,
            history_messages=history_messages,
        )

    def ask_detailed_streaming(self, prompt: str, on_thinking=None, on_tool_event=None, history_messages=None):
        """Comme ``ask_detailed``, mais la réflexion est diffusée EN TEMPS RÉEL.

        ``on_thinking`` (optionnel) est invoqué pour chaque fragment de la
        trace de raisonnement dès sa production par le LLM (stream).
        ``on_tool_event`` (optionnel) est invoqué avec un dict décrivant chaque
        appel d'outil : ``{"event": "tool_start", "tool", "args"}`` avant
        l'exécution puis ``{"event": "tool_result", "tool", "status",
        "summary", "duration_ms"}`` après (status « ok » ou « error »).
        ``history_messages`` (optionnel) : mémoire de session rejouée en
        contexte (voir AgentCore.run_detailed).
        """
        return self.agent.run_detailed(
            prompt,
            on_thinking=on_thinking,
            on_tool_event=on_tool_event,
            history_messages=history_messages,
        )

    def run(
        self,
        prompt: str,
        resume_request_id: str | None = None,
        on_thinking=None,
        on_tool_event=None,
        history_messages=None,
    ):
        """Exécution complète avec reprise et callbacks temps réel.

        ``resume_request_id`` relance une tâche mise en attente par le gate de
        validation après approbation humaine. ``history_messages`` (optionnel) :
        mémoire de session rejouée en contexte. Retour : ``AgentResult``.
        """
        return self.agent.run_detailed(
            prompt,
            on_thinking=on_thinking,
            resume_request_id=resume_request_id,
            on_tool_event=on_tool_event,
            history_messages=history_messages,
        )
