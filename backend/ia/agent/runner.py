class AgentRunner:
    """Fine enveloppe autour d'AgentCore consommée par core.agent_cache.

    ``ask`` conserve le comportement historique (réponse finale seule, str).
    ``ask_detailed`` expose en plus la trace de réflexion collectée quand le
    mode « Réflexion » est activé (AgentCore(enable_thinking=True)).

    **Extension Event Bus** : cette classe expose désormais l'Event Bus interne
    via ``get_event_bus()`` pour permettre l'enregistrement de listeners
    externes (métriques, logging, alerting) sans modifier l'agent.
    """

    def __init__(self, agent):
        self.agent = agent

    # --- Accès à l'infrastructure d'extension -------------------------------

    def get_event_bus(self):
        """Retourne l'instance globale de l'Event Bus (ou None si indisponible).

        Permet aux consommateurs externes d'enregistrer des listeners :

        >>> runner.get_event_bus().on("tool_call", my_handler)
        """
        try:
            from .event_bus import get_event_bus as _get_bus
            return _get_bus()
        except ImportError:
            return None

    def on(self, event_type: str, handler):
        """Raccourci pour enregistrer un listener sur l'Event Bus.

        Retourne True si l'enregistrement a réussi, False sinon.
        """
        bus = self.get_event_bus()
        if bus is None:
            return False
        bus.on(event_type, handler)
        return True

    def off(self, event_type: str, handler):
        """Raccourci pour désenregistrer un listener de l'Event Bus."""
        bus = self.get_event_bus()
        if bus is not None:
            bus.off(event_type, handler)

    # --- Méthodes historiques (inchangées) ----------------------------------

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
